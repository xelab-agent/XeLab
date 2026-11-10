//! Public main-router Client management and browser credential regression on
//! the sequential PostgreSQL schema. Bearer/no-Origin requests model the CLI's
//! independent recovery transport; no CLI process or OCI UI execution is claimed.
use super::*;
use axum::http::HeaderValue;

fn management_request(
    f: &Fixture,
    operation: &str,
    parameters: &[(&str, String)],
    body: Value,
) -> Request<Body> {
    let mut selected = None;
    for (path, methods) in contracts::openapi()["paths"].as_object().unwrap() {
        for (method, spec) in methods.as_object().unwrap() {
            if spec["operationId"] == operation {
                selected = Some((path.clone(), method.to_uppercase()));
            }
        }
    }
    let (mut path, method) = selected.expect("published operation must exist");
    for (name, value) in parameters {
        path = path.replace(&format!("{{{name}}}"), value);
    }
    let mut request = f
        .request(&method, &path)
        .header("idempotency-key", Uuid::new_v4().to_string());
    let bytes = if body.is_null() {
        Vec::new()
    } else {
        request = request.header("content-type", "application/json");
        serde_json::to_vec(&body).unwrap()
    };
    request.body(Body::from(bytes)).unwrap()
}

fn bearer(mut request: Request<Body>, secret: &str) -> Request<Body> {
    request.headers_mut().insert(
        "authorization",
        HeaderValue::from_str(&format!("Bearer {secret}")).unwrap(),
    );
    request
}

fn browser_origin(mut request: Request<Body>, origin: &str) -> Request<Body> {
    request
        .headers_mut()
        .insert("origin", HeaderValue::from_str(origin).unwrap());
    request
}

fn revision(mut request: Request<Body>, revision: i64) -> Request<Body> {
    request.headers_mut().insert(
        "if-match",
        HeaderValue::from_str(&format!("\"rev-{revision}\"")).unwrap(),
    );
    request
}

fn owner_cookie(mut request: Request<Body>, credential: &str) -> Request<Body> {
    request.headers_mut().insert(
        "cookie",
        HeaderValue::from_str(&format!("{AUTH_COOKIE}={credential}")).unwrap(),
    );
    request
}

fn client_spec(f: &Fixture, name: &str) -> Value {
    json!({"name":name,"plugin_id":"org.xelab.webui",
        "redirect_uris":[format!("{}/callback",f.ui)],"allowed_origins":[f.ui],
        "maximum_rules":[{"action":"account.read","resource":{"kind":"account","match":"all"}}]})
}

#[tokio::test]
async fn main_router_client_crud_etags_oauth_scope_and_cli_recovery_after_disable() {
    let Some(mut f) =
        Fixture::new("https://kernel.example.org:8443", "https://ui.example.org").await
    else {
        return;
    };
    let specification = client_spec(&f, "Published Client lifecycle");
    let created = f
        .call(bearer(
            management_request(&f, "createClient", &[], specification.clone()),
            &f.session,
        ))
        .await;
    assert_eq!(created.status(), 201);
    let created = value(created).await;
    validate("Client", &created).unwrap();
    let id = contracts::id(&created, "id").unwrap();
    let public_id = created["client_id"].as_str().unwrap().to_owned();
    let params = [("client_id", id.to_string())];
    let read = f
        .call(bearer(
            management_request(&f, "getClient", &params, Value::Null),
            &f.session,
        ))
        .await;
    assert_eq!(read.status(), 200);
    assert_eq!(value(read).await, created);
    let listed = f
        .call(bearer(
            management_request(&f, "listClients", &[], Value::Null),
            &f.session,
        ))
        .await;
    assert_eq!(listed.status(), 200);
    let listed = value(listed).await;
    validate("ClientPage", &listed).unwrap();
    assert!(listed["items"]
        .as_array()
        .unwrap()
        .iter()
        .any(|client| client["id"] == id.to_string()));
    let mut changed = specification.clone();
    changed["name"] = json!("Published Client updated");
    let stale = f
        .call(revision(
            bearer(
                management_request(&f, "updateClient", &params, changed.clone()),
                &f.session,
            ),
            created["revision"].as_i64().unwrap() + 1,
        ))
        .await;
    assert_eq!(stale.status(), 412);
    assert_eq!(value(stale).await["error"]["code"], "ETAG_MISMATCH");
    let updated = f
        .call(revision(
            bearer(
                management_request(&f, "updateClient", &params, changed.clone()),
                &f.session,
            ),
            created["revision"].as_i64().unwrap(),
        ))
        .await;
    assert_eq!(updated.status(), 200);
    let updated = value(updated).await;
    validate("Client", &updated).unwrap();
    assert_eq!(updated["id"], created["id"]);
    assert_eq!(updated["client_id"], public_id);
    assert_eq!(updated["spec"], changed);
    assert_eq!(
        updated["revision"].as_i64().unwrap(),
        created["revision"].as_i64().unwrap() + 1
    );
    // Use this publicly created/updated Client for the actual existing HTTP
    // authorization-code flow, rather than inserting synthetic credentials.
    f.client = updated.clone();
    let tokens = f.tokens().await;
    let denied = f
        .call(browser_origin(
            bearer(
                management_request(
                    &f,
                    "createClient",
                    &[],
                    client_spec(&f, "Scope cannot escalate"),
                ),
                tokens["access_token"].as_str().unwrap(),
            ),
            &f.ui,
        ))
        .await;
    assert_eq!(
        denied.status(),
        403,
        "account.read OAuth scope cannot manage Clients"
    );
    assert_eq!(value(denied).await["error"]["code"], "PERMISSION_DENIED");
    let stale = f
        .call(revision(
            bearer(
                management_request(&f, "disableClient", &params, Value::Null),
                &f.session,
            ),
            created["revision"].as_i64().unwrap(),
        ))
        .await;
    assert_eq!(stale.status(), 412);
    let disabled = f
        .call(revision(
            bearer(
                management_request(&f, "disableClient", &params, Value::Null),
                &f.session,
            ),
            updated["revision"].as_i64().unwrap(),
        ))
        .await;
    assert_eq!(disabled.status(), 204);
    let disabled = f
        .call(bearer(
            management_request(&f, "getClient", &params, Value::Null),
            &f.session,
        ))
        .await;
    assert_eq!(disabled.status(), 200);
    let disabled = value(disabled).await;
    assert_eq!(disabled["status"], "disabled");
    assert_eq!(disabled["client_id"], public_id);
    let rejected = f
        .token(&[
            ("grant_type", "refresh_token"),
            ("client_id", &public_id),
            ("refresh_token", tokens["refresh_token"].as_str().unwrap()),
        ])
        .await;
    assert_eq!(rejected.status(), 401);
    let rejected = value(rejected).await;
    validate("OAuthError", &rejected).unwrap();
    assert_eq!(rejected["error"], "invalid_client");
    let account = [("account_id", f.owner.to_string())];
    let cli = f
        .call(bearer(
            management_request(&f, "getAccount", &account, Value::Null),
            &f.session,
        ))
        .await;
    assert_eq!(
        cli.status(),
        200,
        "Owner CLI-style recovery remains independent of disabled UI Client"
    );
    assert_eq!(value(cli).await["id"], f.owner.to_string());
    let c = f.app.db.connect().await.unwrap();
    let audits = c.query("SELECT action,outcome,resource FROM audit_events WHERE action IN ('createClient','updateClient','disableClient')", &[]).await.unwrap();
    for action in ["createClient", "updateClient", "disableClient"] {
        assert!(audits
            .iter()
            .any(|row| row.get::<_, String>("action") == action
                && row.get::<_, String>("outcome") == "succeeded"));
    }
    for row in audits {
        let resource: Value = row.get("resource");
        assert_eq!(resource["kind"], "client");
        if row.get::<_, String>("outcome") == "succeeded" {
            assert_eq!(resource["ids"][0], id.to_string());
        }
    }
}

#[tokio::test]
async fn main_router_client_cookie_csrf_and_registered_foreign_origin_credentials_fail_closed() {
    let Some(f) = Fixture::new("https://kernel.example.org:8443", "https://ui.example.org").await
    else {
        return;
    };
    let spec = client_spec(&f, "Cookie-managed Client");
    let c = f.app.db.connect().await.unwrap();
    let before: i64 = c
        .query_one("SELECT count(*) FROM clients", &[])
        .await
        .unwrap()
        .get(0);
    let missing_csrf = f
        .call(browser_origin(
            owner_cookie(
                management_request(&f, "createClient", &[], spec.clone()),
                &f.session,
            ),
            f.core(),
        ))
        .await;
    assert_eq!(missing_csrf.status(), 403);
    assert_eq!(
        c.query_one("SELECT count(*) FROM clients", &[])
            .await
            .unwrap()
            .get::<_, i64>(0),
        before
    );
    let mut valid = browser_origin(
        owner_cookie(
            management_request(&f, "createClient", &[], spec),
            &f.session,
        ),
        f.core(),
    );
    valid.headers_mut().insert(
        "x-csrf-token",
        HeaderValue::from_str(&f.app.auth.csrf_token(&f.session)).unwrap(),
    );
    let valid = f.call(valid).await;
    assert_eq!(valid.status(), 201);
    let cookie_client = value(valid).await;
    validate("Client", &cookie_client).unwrap();
    let raw_session = f
        .call(browser_origin(
            bearer(
                management_request(&f, "listClients", &[], Value::Null),
                &f.session,
            ),
            &f.ui,
        ))
        .await;
    assert_eq!(
        raw_session.status(),
        403,
        "registered UI Origin cannot use a raw session Bearer"
    );
    let foreign_cookie = f
        .call(browser_origin(
            owner_cookie(
                management_request(&f, "listClients", &[], Value::Null),
                &f.session,
            ),
            &f.ui,
        ))
        .await;
    assert_eq!(
        foreign_cookie.status(),
        403,
        "registered UI Origin cannot use an ambient core Cookie"
    );
    let key_spec = json!({"name":"Origin classification key","inheritance":{"mode":"selected","rules":[{"action":"client.read","resource":{"kind":"client","match":"all"}}]},"conditions":{"expires_at":chrono::Utc::now()+chrono::Duration::days(1)}});
    let key = f
        .call(bearer(
            management_request(
                &f,
                "createApiKey",
                &[("account_id", f.owner.to_string())],
                key_spec,
            ),
            &f.session,
        ))
        .await;
    assert_eq!(key.status(), 201);
    let key = value(key).await;
    let secret = key["secret"].as_str().unwrap();
    let cli_key = f
        .call(bearer(
            management_request(&f, "listClients", &[], Value::Null),
            secret,
        ))
        .await;
    assert_eq!(
        cli_key.status(),
        200,
        "the API key is valid and has Client read authority"
    );
    assert!(value(cli_key).await["items"]
        .as_array()
        .unwrap()
        .iter()
        .any(|client| client["id"] == cookie_client["id"]));
    let foreign_key = f
        .call(browser_origin(
            bearer(
                management_request(&f, "listClients", &[], Value::Null),
                secret,
            ),
            &f.ui,
        ))
        .await;
    assert_eq!(
        foreign_key.status(),
        403,
        "registered UI Origin requires direct OAuth access, not API keys"
    );
    let foreign_key_cookie = f
        .call(browser_origin(
            owner_cookie(
                management_request(&f, "listClients", &[], Value::Null),
                secret,
            ),
            &f.ui,
        ))
        .await;
    assert_eq!(foreign_key_cookie.status(), 403);
    assert_eq!(
        c.query_one("SELECT count(*) FROM clients", &[])
            .await
            .unwrap()
            .get::<_, i64>(0),
        before + 1
    );
}

#[tokio::test]
async fn session_list_can_return_empty_page_with_a_later_active_session() {
    let Some(f) = Fixture::new("https://kernel.example.org:8443", "https://ui.example.org").await
    else {
        return;
    };
    let c = f.app.db.connect().await.unwrap();
    // Insert only disposable, non-session metadata into this isolated test DB.
    // Their earlier timestamps force the public endpoint to scan an empty page.
    c.execute(
        "INSERT INTO credentials(id,owner_id,kind,hmac_key_id,verification_digest,status,conditions,expires_at,created_at,updated_at) \
         SELECT gen_random_uuid(),$1,'api_key','page-fixture',decode(md5(n::text),'hex'),'expired','{}'::jsonb, \
                now()+interval '1 hour',now()-interval '2 days',now() \
         FROM generate_series(1,201) AS n",
        &[&f.owner],
    ).await.unwrap();
    let first = f
        .call(bearer(
            f.request("GET", "/api/platform/v1/sessions?limit=200")
                .body(Body::empty())
                .unwrap(),
            &f.session,
        ))
        .await;
    assert_eq!(first.status(), 200);
    let first = value(first).await;
    validate("SessionPage", &first).unwrap();
    assert_eq!(first["items"].as_array().unwrap().len(), 0);
    let cursor = first["next_cursor"]
        .as_str()
        .expect("filtered page must retain a cursor");
    let query = url::form_urlencoded::Serializer::new(String::new())
        .append_pair("limit", "200")
        .append_pair("cursor", cursor)
        .finish();
    let next = f
        .call(bearer(
            f.request("GET", &format!("/api/platform/v1/sessions?{query}"))
                .body(Body::empty())
                .unwrap(),
            &f.session,
        ))
        .await;
    assert_eq!(next.status(), 200);
    let next = value(next).await;
    validate("SessionPage", &next).unwrap();
    assert!(next["items"]
        .as_array()
        .unwrap()
        .iter()
        .any(|item| item["account_id"] == f.owner.to_string() && item["status"] == "active"));
}
