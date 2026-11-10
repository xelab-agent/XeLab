use std::{
    collections::VecDeque,
    sync::{Arc, Mutex},
    time::Duration,
};

use rcgen::{generate_simple_self_signed, CertifiedKey};
use rustls::{pki_types::PrivatePkcs8KeyDer, ServerConfig};
use serde_json::json;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
    task::JoinHandle,
    time::timeout,
};
use tokio_rustls::TlsAcceptor;
use xelab_api_client_example::{ClientError, XeLabClient};

struct Reply {
    status: u16,
    content_type: &'static str,
    body: String,
    extra_headers: String,
    delay: Duration,
    chunked: bool,
}

impl Reply {
    fn json(status: u16, body: serde_json::Value) -> Self {
        Self {
            status,
            content_type: "application/json",
            body: body.to_string(),
            extra_headers: String::new(),
            delay: Duration::ZERO,
            chunked: false,
        }
    }
}

struct TlsFixture {
    port: u16,
    ca_pem: String,
    requests: Arc<Mutex<Vec<String>>>,
    task: JoinHandle<()>,
}

impl TlsFixture {
    async fn start(replies: Vec<Reply>) -> Self {
        let CertifiedKey { cert, key_pair } =
            generate_simple_self_signed(vec!["localhost".to_owned()]).unwrap();
        let config =
            ServerConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
                .with_safe_default_protocol_versions()
                .unwrap()
                .with_no_client_auth()
                .with_single_cert(
                    vec![cert.der().clone()],
                    PrivatePkcs8KeyDer::from(key_pair.serialize_der()).into(),
                )
                .unwrap();
        let acceptor = TlsAcceptor::from(Arc::new(config));
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let observed = requests.clone();
        let mut replies = VecDeque::from(replies);
        let task = tokio::spawn(async move {
            while let Ok((socket, _)) = listener.accept().await {
                let Ok(mut stream) = acceptor.accept(socket).await else {
                    continue;
                };
                let mut request = Vec::new();
                let mut buffer = [0u8; 1024];
                while let Ok(read) = stream.read(&mut buffer).await {
                    if read == 0 {
                        break;
                    }
                    request.extend_from_slice(&buffer[..read]);
                    if request.windows(4).any(|part| part == b"\r\n\r\n") {
                        break;
                    }
                    if request.len() > 16_384 {
                        break;
                    }
                }
                observed
                    .lock()
                    .unwrap()
                    .push(String::from_utf8_lossy(&request).into_owned());
                let Some(reply) = replies.pop_front() else {
                    continue;
                };
                tokio::time::sleep(reply.delay).await;
                let framing = if reply.chunked {
                    "Transfer-Encoding: chunked\r\n".to_owned()
                } else {
                    format!("Content-Length: {}\r\n", reply.body.len())
                };
                let headers = format!(
                    "HTTP/1.1 {} Fixture\r\nContent-Type: {}\r\n{}Connection: close\r\n{}\r\n",
                    reply.status, reply.content_type, framing, reply.extra_headers,
                );
                if stream.write_all(headers.as_bytes()).await.is_ok() {
                    if reply.chunked {
                        for chunk in reply.body.as_bytes().chunks(65_536) {
                            if stream
                                .write_all(format!("{:x}\r\n", chunk.len()).as_bytes())
                                .await
                                .is_err()
                                || stream.write_all(chunk).await.is_err()
                                || stream.write_all(b"\r\n").await.is_err()
                            {
                                break;
                            }
                        }
                        let _ = stream.write_all(b"0\r\n\r\n").await;
                    } else {
                        let _ = stream.write_all(reply.body.as_bytes()).await;
                    }
                }
                // Complete the TLS write side so the client can distinguish
                // an oversized body from a truncated fixture connection.
                let _ = stream.shutdown().await;
            }
        });
        Self {
            port,
            ca_pem: cert.pem(),
            requests,
            task,
        }
    }

    fn origin(&self) -> String {
        format!("https://localhost:{}", self.port)
    }

    fn client(&self) -> XeLabClient {
        XeLabClient::new(
            &self.origin(),
            "fixture-token",
            self.ca_pem.as_bytes(),
            Duration::from_secs(3),
        )
        .unwrap()
    }

    fn requests(&self) -> Vec<String> {
        self.requests.lock().unwrap().clone()
    }
}

impl Drop for TlsFixture {
    fn drop(&mut self) {
        self.task.abort();
    }
}

fn node() -> serde_json::Value {
    json!({
        "id": "00000000-0000-4000-8000-000000000001", "revision": 1,
        "created_at": "2026-10-28T12:00:00Z", "updated_at": "2026-10-28T12:00:00Z",
        "config": {"kernel_cpu_millis":4000, "kernel_memory_bytes":17179869184u64,
                   "system_cpu_millis":1000, "system_memory_bytes":1073741824,
                   "shutdown_grace_ms":30000},
        "host_cpu_millis":8000, "host_memory_bytes":34359738368u64,
        "plugin_cpu_pool_millis":3000, "plugin_memory_pool_bytes":16106127360u64,
        "reserved_cpu_millis":0, "reserved_memory_bytes":0, "admission_enabled":true
    })
}

fn error(code: &str) -> serde_json::Value {
    json!({"error":{"code":code,"message":"fixture denied","retryable":false,
                    "violations":[],"retry_after_ms":null},
           "request_id":"00000000-0000-4000-8000-000000000001"})
}

#[test]
fn fixture_fields_and_operations_match_the_checked_in_openapi() {
    let contract: serde_json::Value =
        serde_json::from_str(include_str!("../../../docs/contracts/openapi.json")).unwrap();
    let node_op = &contract["paths"]["/api/platform/v1/node"]["get"];
    let pages_op = &contract["paths"]["/api/platform/v1/deployments"]["get"];
    assert_eq!(node_op["operationId"], "getNode");
    assert_eq!(pages_op["operationId"], "listDeployments");
    assert_eq!(node_op["security"][0]["bearerAuth"], json!([]));
    assert_eq!(pages_op["security"][0]["bearerAuth"], json!([]));
    let params = pages_op["parameters"].as_array().unwrap();
    let limit = params
        .iter()
        .find(|value| value["name"] == "limit")
        .unwrap();
    let cursor = params
        .iter()
        .find(|value| value["name"] == "cursor")
        .unwrap();
    assert_eq!(limit["schema"]["minimum"], 1);
    assert_eq!(limit["schema"]["maximum"], 200);
    assert_eq!(cursor["schema"]["maxLength"], 2048);
    let schemas = &contract["components"]["schemas"];
    let valid_node = node();
    let valid_error = error("PERMISSION_DENIED");
    for (schema_name, fixture) in [
        ("NodeStatus", &valid_node),
        ("NodeConfig", &valid_node["config"]),
        ("ErrorEnvelope", &valid_error),
        ("ErrorDetail", &valid_error["error"]),
    ] {
        let schema = &schemas[schema_name];
        let properties = schema["properties"].as_object().unwrap();
        let object = fixture.as_object().unwrap();
        for key in object.keys() {
            assert!(
                properties.contains_key(key),
                "{schema_name}.{key} is not in OpenAPI"
            );
        }
        for field in schema["required"].as_array().unwrap() {
            let field = field.as_str().unwrap();
            assert!(
                object.contains_key(field),
                "{schema_name}.{field} is required"
            );
        }
    }
}

#[tokio::test]
async fn node_and_deployment_pages_use_fixed_paths_and_encoded_cursor() {
    let fixture = TlsFixture::start(vec![
        Reply::json(200, node()),
        Reply::json(200, json!({"items":[],"next_cursor":"next+/= cursor"})),
        Reply::json(200, json!({"items":[],"next_cursor":null})),
    ])
    .await;
    let client = fixture.client();
    let node = client.get_node().await.unwrap();
    assert_eq!(node.revision, 1);
    assert_eq!(node.config.kernel_cpu_millis, 4000);
    assert!(node.admission_enabled);
    let first = client.list_deployments(Some(2), None).await.unwrap();
    assert_eq!(first.next_cursor.as_deref(), Some("next+/= cursor"));
    let second = client
        .list_deployments(Some(2), first.next_cursor.as_deref())
        .await
        .unwrap();
    assert!(second.next_cursor.is_none());
    let requests = fixture.requests();
    assert_eq!(requests.len(), 3);
    assert!(requests[0].starts_with("GET /api/platform/v1/node HTTP/1.1\r\n"));
    for request in &requests {
        assert!(request
            .to_ascii_lowercase()
            .contains("authorization: bearer fixture-token\r\n"));
        assert!(request
            .to_ascii_lowercase()
            .contains("accept: application/json\r\n"));
    }
    let target = requests[2]
        .lines()
        .next()
        .unwrap()
        .split_whitespace()
        .nth(1)
        .unwrap();
    let url = reqwest::Url::parse(&format!("https://localhost{target}")).unwrap();
    assert_eq!(url.path(), "/api/platform/v1/deployments");
    let query: Vec<_> = url
        .query_pairs()
        .map(|(key, value)| (key.into_owned(), value.into_owned()))
        .collect();
    assert!(query.contains(&("limit".into(), "2".into())));
    assert!(query.contains(&("cursor".into(), "next+/= cursor".into())));
}

#[tokio::test]
async fn authentication_and_authorization_errors_preserve_standard_projection() {
    let fixture = TlsFixture::start(vec![
        Reply::json(401, error("INVALID_CREDENTIAL")),
        Reply::json(403, error("PERMISSION_DENIED")),
    ])
    .await;
    let client = fixture.client();
    for (expected_status, expected_code) in
        [(401, "INVALID_CREDENTIAL"), (403, "PERMISSION_DENIED")]
    {
        match client.get_node().await.unwrap_err() {
            ClientError::Http {
                status,
                error: Some(error),
            } => {
                assert_eq!(status, expected_status);
                assert_eq!(error.error.code, expected_code);
                assert!(!error.error.retryable);
                assert_eq!(error.request_id, "00000000-0000-4000-8000-000000000001");
            }
            other => panic!("unexpected error: {other:?}"),
        }
    }
    assert_eq!(fixture.requests().len(), 2, "no automatic retries");
}

#[tokio::test]
async fn client_error_formatting_does_not_print_server_echoed_credentials() {
    let mut echoed = error("PERMISSION_DENIED");
    echoed["error"]["message"] = json!("fixture-token");
    let fixture = TlsFixture::start(vec![Reply::json(403, echoed)]).await;
    let error = fixture.client().get_node().await.unwrap_err();
    assert!(!format!("{error:?} {error}").contains("fixture-token"));
    match error {
        ClientError::Http {
            error: Some(body), ..
        } => {
            assert_eq!(body.error.message, "fixture-token");
        }
        other => panic!("unexpected error: {other:?}"),
    }
}

#[tokio::test]
async fn malformed_error_and_invalid_success_json_are_distinct() {
    let fixture = TlsFixture::start(vec![
        Reply {
            status: 500,
            content_type: "application/json",
            body: "not json".into(),
            extra_headers: String::new(),
            delay: Duration::ZERO,
            chunked: false,
        },
        Reply {
            status: 200,
            content_type: "application/json",
            body: "{".into(),
            extra_headers: String::new(),
            delay: Duration::ZERO,
            chunked: false,
        },
        Reply {
            status: 200,
            content_type: "text/plain",
            body: node().to_string(),
            extra_headers: String::new(),
            delay: Duration::ZERO,
            chunked: false,
        },
    ])
    .await;
    let client = fixture.client();
    assert!(matches!(
        client.get_node().await,
        Err(ClientError::Http {
            status: 500,
            error: None
        })
    ));
    assert!(matches!(
        client.get_node().await,
        Err(ClientError::InvalidResponse)
    ));
    assert!(matches!(
        client.get_node().await,
        Err(ClientError::InvalidResponse)
    ));
}

#[tokio::test]
async fn invalid_page_parameters_are_rejected_without_requests() {
    let fixture = TlsFixture::start(vec![]).await;
    let client = fixture.client();
    for limit in [0, 201] {
        assert!(matches!(
            client.list_deployments(Some(limit), None).await,
            Err(ClientError::InvalidParameter(_))
        ));
    }
    for cursor in [String::new(), "x".repeat(2049)] {
        assert!(matches!(
            client.list_deployments(None, Some(&cursor)).await,
            Err(ClientError::InvalidParameter(_))
        ));
    }
    assert!(fixture.requests().is_empty());
}

#[tokio::test]
async fn tls_trust_hostname_and_http_scheme_are_enforced() {
    let fixture = TlsFixture::start(vec![]).await;
    let other = generate_simple_self_signed(vec!["localhost".to_owned()]).unwrap();
    let wrong_ca = XeLabClient::new(
        &fixture.origin(),
        "fixture-token",
        other.cert.pem().as_bytes(),
        Duration::from_secs(3),
    )
    .unwrap();
    assert!(matches!(
        wrong_ca.get_node().await,
        Err(ClientError::Transport)
    ));
    let wrong_host = XeLabClient::new(
        &format!("https://127.0.0.1:{}", fixture.port),
        "fixture-token",
        fixture.ca_pem.as_bytes(),
        Duration::from_secs(3),
    )
    .unwrap();
    assert!(matches!(
        wrong_host.get_node().await,
        Err(ClientError::Transport)
    ));
    let plain = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let plain_origin = format!("http://{}", plain.local_addr().unwrap());
    assert!(matches!(
        XeLabClient::new(
            &plain_origin,
            "fixture-token",
            fixture.ca_pem.as_bytes(),
            Duration::from_secs(3)
        ),
        Err(ClientError::InvalidConfiguration(_))
    ));
    assert!(timeout(Duration::from_millis(100), plain.accept())
        .await
        .is_err());
    assert!(fixture.requests().is_empty());
}

#[tokio::test]
async fn timeout_and_response_size_are_bounded() {
    let mut slow = Reply::json(200, node());
    slow.delay = Duration::from_millis(700);
    let fixture = TlsFixture::start(vec![slow]).await;
    let client = XeLabClient::new(
        &fixture.origin(),
        "fixture-token",
        fixture.ca_pem.as_bytes(),
        Duration::from_millis(200),
    )
    .unwrap();
    assert!(matches!(client.get_node().await, Err(ClientError::Timeout)));
    for chunked in [false, true] {
        let large = TlsFixture::start(vec![Reply {
            status: 200,
            content_type: "application/json",
            body: "x".repeat(4 * 1024 * 1024 + 1),
            extra_headers: String::new(),
            delay: Duration::ZERO,
            chunked,
        }])
        .await;
        let result = large.client().get_node().await;
        assert!(
            matches!(&result, Err(ClientError::ResponseTooLarge)),
            "chunked={chunked}; outcome={result:?}"
        );
    }
}

#[tokio::test]
async fn redirects_do_not_send_bearer_to_another_origin() {
    let target = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let mut redirect = Reply::json(302, json!({}));
    redirect.extra_headers = format!(
        "Location: http://{}/stolen\r\n",
        target.local_addr().unwrap()
    );
    let fixture = TlsFixture::start(vec![redirect]).await;
    assert!(matches!(
        fixture.client().get_node().await,
        Err(ClientError::Http { status: 302, .. })
    ));
    assert!(timeout(Duration::from_millis(100), target.accept())
        .await
        .is_err());
}
