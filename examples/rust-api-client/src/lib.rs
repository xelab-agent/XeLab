//! An independent, deliberately small read-only XeLab API client.
//!
//! Only `getNode` and `listDeployments` are supported. The caller supplies a
//! trusted CA and a bearer credential; the client never logs either one.

use std::{fmt, time::Duration};

use reqwest::{header, redirect::Policy, Certificate, Client, Response, Url};
use serde::Deserialize;
use serde_json::Value;

const MAX_RESPONSE_BYTES: usize = 4 * 1024 * 1024;

/// A transport, protocol, or server error. Server responses are never retried.
pub enum ClientError {
    InvalidConfiguration(&'static str),
    InvalidParameter(&'static str),
    Timeout,
    Transport,
    InvalidResponse,
    ResponseTooLarge,
    Http {
        status: u16,
        error: Option<Box<ErrorEnvelope>>,
    },
}

impl fmt::Debug for ClientError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidConfiguration(message) => f
                .debug_tuple("InvalidConfiguration")
                .field(message)
                .finish(),
            Self::InvalidParameter(message) => {
                f.debug_tuple("InvalidParameter").field(message).finish()
            }
            Self::Timeout => f.write_str("Timeout"),
            Self::Transport => f.write_str("Transport"),
            Self::InvalidResponse => f.write_str("InvalidResponse"),
            Self::ResponseTooLarge => f.write_str("ResponseTooLarge"),
            Self::Http { status, error } => f
                .debug_struct("Http")
                .field("status", status)
                .field("has_error_envelope", &error.is_some())
                .finish(),
        }
    }
}

impl fmt::Display for ClientError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidConfiguration(message) => write!(f, "invalid configuration: {message}"),
            Self::InvalidParameter(message) => write!(f, "invalid parameter: {message}"),
            Self::Timeout => f.write_str("request timed out"),
            Self::Transport => f.write_str("HTTPS transport failed"),
            Self::InvalidResponse => f.write_str("invalid JSON response"),
            Self::ResponseTooLarge => f.write_str("response exceeds the client limit"),
            Self::Http { status, .. } => write!(f, "HTTP {status}"),
        }
    }
}

impl std::error::Error for ClientError {}

fn transport_error(error: reqwest::Error) -> ClientError {
    if error.is_timeout() {
        ClientError::Timeout
    } else {
        // Do not expose request URLs, headers, or internal error strings.
        ClientError::Transport
    }
}

/// Public `ErrorEnvelope` projection from `docs/contracts/openapi.json`.
#[derive(Debug, Deserialize)]
pub struct ErrorEnvelope {
    pub error: ErrorDetail,
    pub request_id: String,
}

#[derive(Debug, Deserialize)]
pub struct ErrorDetail {
    pub code: String,
    pub message: String,
    pub retryable: bool,
    pub violations: Vec<FieldViolation>,
    pub retry_after_ms: Option<u64>,
    pub resource_id: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct FieldViolation {
    pub path: String,
    pub reason: String,
}

/// The `NodeStatus` and `NodeConfig` fields in the current OpenAPI document.
#[derive(Debug, Deserialize)]
pub struct NodeStatus {
    pub id: String,
    pub revision: u32,
    pub created_at: String,
    pub updated_at: String,
    pub config: NodeConfig,
    pub host_cpu_millis: u64,
    pub host_memory_bytes: u64,
    pub plugin_cpu_pool_millis: u64,
    pub plugin_memory_pool_bytes: u64,
    pub reserved_cpu_millis: u64,
    pub reserved_memory_bytes: u64,
    pub admission_enabled: bool,
}

#[derive(Debug, Deserialize)]
pub struct NodeConfig {
    pub kernel_cpu_millis: u64,
    pub kernel_memory_bytes: u64,
    pub system_cpu_millis: u64,
    pub system_memory_bytes: u64,
    pub shutdown_grace_ms: u64,
}

/// `DeploymentPage` envelope. Items are raw JSON, not a generated SDK model.
#[derive(Debug, Deserialize)]
pub struct DeploymentPage {
    pub items: Vec<Value>,
    // Explicit null means the final page; a missing field is not the same.
    #[serde(deserialize_with = "required_nullable_cursor")]
    pub next_cursor: Option<String>,
}

fn required_nullable_cursor<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    Option::<String>::deserialize(deserializer)
}

/// A client bound to one exact HTTPS origin and one explicit CA.
///
/// It intentionally does not implement `Debug`, to avoid printing credentials.
pub struct XeLabClient {
    origin: Url,
    client: Client,
    authorization: header::HeaderValue,
}

impl XeLabClient {
    pub fn new(
        origin: &str,
        bearer: &str,
        ca_pem: &[u8],
        timeout: Duration,
    ) -> Result<Self, ClientError> {
        let origin = Url::parse(origin)
            .map_err(|_| ClientError::InvalidConfiguration("expected an HTTPS origin"))?;
        if origin.scheme() != "https"
            || origin.host_str().is_none()
            || !origin.username().is_empty()
            || origin.password().is_some()
            || origin.path() != "/"
            || origin.query().is_some()
            || origin.fragment().is_some()
        {
            return Err(ClientError::InvalidConfiguration(
                "expected an HTTPS origin without credentials, path, query, or fragment",
            ));
        }
        if timeout.is_zero() {
            return Err(ClientError::InvalidConfiguration(
                "timeout must be positive",
            ));
        }
        // RFC 6750 b64token syntax; reject whitespace and control characters.
        if bearer.is_empty()
            || bearer.len() > 8192
            || !bearer
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || b"-._~+/=".contains(&byte))
        {
            return Err(ClientError::InvalidConfiguration(
                "invalid bearer credential",
            ));
        }
        let mut authorization = header::HeaderValue::from_str(&format!("Bearer {bearer}"))
            .map_err(|_| ClientError::InvalidConfiguration("invalid bearer credential"))?;
        authorization.set_sensitive(true);
        let ca = Certificate::from_pem(ca_pem)
            .map_err(|_| ClientError::InvalidConfiguration("invalid CA certificate"))?;
        let client = Client::builder()
            .tls_built_in_root_certs(false)
            .add_root_certificate(ca)
            .redirect(Policy::none())
            .timeout(timeout)
            .connect_timeout(timeout)
            .build()
            .map_err(|_| ClientError::InvalidConfiguration("could not build HTTPS client"))?;
        Ok(Self {
            origin,
            client,
            authorization,
        })
    }

    /// Read `GET /api/platform/v1/node` (`getNode`).
    pub async fn get_node(&self) -> Result<NodeStatus, ClientError> {
        let response = self
            .get("/api/platform/v1/node")
            .send()
            .await
            .map_err(transport_error)?;
        decode(response).await
    }

    /// Read one page of `GET /api/platform/v1/deployments` (`listDeployments`).
    /// Pass a returned cursor only with the same account and query scope.
    pub async fn list_deployments(
        &self,
        limit: Option<u16>,
        cursor: Option<&str>,
    ) -> Result<DeploymentPage, ClientError> {
        if limit.is_some_and(|value| !(1..=200).contains(&value)) {
            return Err(ClientError::InvalidParameter(
                "limit must be between 1 and 200",
            ));
        }
        if cursor.is_some_and(|value| value.is_empty() || value.chars().count() > 2048) {
            return Err(ClientError::InvalidParameter(
                "cursor must contain 1 to 2048 characters",
            ));
        }
        let mut request = self.get("/api/platform/v1/deployments");
        if let Some(value) = limit {
            request = request.query(&[("limit", value)]);
        }
        if let Some(value) = cursor {
            request = request.query(&[("cursor", value)]);
        }
        decode(request.send().await.map_err(transport_error)?).await
    }

    fn get(&self, path: &str) -> reqwest::RequestBuilder {
        // Both paths are fixed internal constants; callers cannot supply URLs.
        let url = self.origin.join(path).expect("fixed API path is valid");
        self.client
            .get(url)
            .header(header::AUTHORIZATION, self.authorization.clone())
            .header(header::ACCEPT, "application/json")
    }
}

async fn decode<T: serde::de::DeserializeOwned>(mut response: Response) -> Result<T, ClientError> {
    let status = response.status();
    let json = response
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(';').next())
        .is_some_and(|value| value.trim().eq_ignore_ascii_case("application/json"));
    if response
        .content_length()
        .is_some_and(|length| length > MAX_RESPONSE_BYTES as u64)
    {
        return Err(ClientError::ResponseTooLarge);
    }
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(transport_error)? {
        if chunk.len() > MAX_RESPONSE_BYTES - body.len() {
            return Err(ClientError::ResponseTooLarge);
        }
        body.extend_from_slice(&chunk);
    }
    if status.as_u16() != 200 {
        let error = if json {
            serde_json::from_slice::<ErrorEnvelope>(&body)
                .ok()
                .map(Box::new)
        } else {
            None
        };
        return Err(ClientError::Http {
            status: status.as_u16(),
            error,
        });
    }
    if !json {
        return Err(ClientError::InvalidResponse);
    }
    serde_json::from_slice(&body).map_err(|_| ClientError::InvalidResponse)
}
