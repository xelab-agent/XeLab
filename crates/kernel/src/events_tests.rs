//! These tests execute real PostgreSQL transactions and reconnect after aborted
//! writers. They do not substitute for the independently tracked host reboot or
//! production capacity acceptance.
use super::*;
use crate::{backups::tests::Fixture, contracts, db};
use axum::http::HeaderMap;
use serde_json::{json, Value};
use std::collections::HashMap;
use uuid::Uuid;

struct EventFixture {
    base: Fixture,
    ctx: crate::auth::Context,
    topic: String,
}
impl EventFixture {
    async fn new() -> Option<Self> {
        let base = Fixture::new().await?;
        let c = base.app.db.connect().await.unwrap();
        let ctx = base
            .app
            .auth
            .authenticate(
                &c,
                &base.token,
                "127.0.0.1".parse().unwrap(),
                Uuid::new_v4(),
            )
            .await
            .unwrap();
        let installation: Uuid = c
            .query_one(
                "SELECT id FROM installations WHERE plugin_id='org.xelab.backup-fixture'",
                &[],
            )
            .await
            .unwrap()
            .get(0);
        let topic = "org.xelab.backup-fixture.completed".to_owned();
        let schema = json!({"type":"object","properties":{"value":{"type":"integer"}},"required":["value"],"additionalProperties":false});
        let digest = contracts::digest(&serde_json::to_vec(&schema).unwrap());
        let manifest = json!({"plugin_id":"org.xelab.backup-fixture","event_topics":[{"topic":topic,"version":"1.0.0","payload_schema":schema,"schema_digest":digest}]});
        c.execute(
            "UPDATE installations SET manifest=$2 WHERE id=$1",
            &[&installation, &manifest],
        )
        .await
        .unwrap();
        c.execute("INSERT INTO event_contracts(topic,version,publisher_installation_id,schema_digest,payload_schema) VALUES($1,'1.0.0',$2,$3,$4)", &[&topic,&installation,&digest,&schema]).await.unwrap();
        Some(Self { base, ctx, topic })
    }
    fn body(&self, key: &str, value: i64) -> Value {
        json!({"topic":self.topic,"version":"1.0.0","payload":{"value":value},"dedupe_key":key})
    }
    async fn publish(&self, key: &str, value: i64) -> Value {
        let mut c = self.base.app.db.connect().await.unwrap();
        let tx = c.transaction().await.unwrap();
        db::auth_lock(&tx, false).await.unwrap();
        let (event, duplicate) =
            append(&tx, &self.base.app.auth, &self.ctx, &self.body(key, value))
                .await
                .unwrap();
        assert!(!duplicate);
        tx.commit().await.unwrap();
        event
    }
    async fn subscription(&self, start: &str, attempts: i64) -> Uuid {
        let mut c = self.base.app.db.connect().await.unwrap();
        let tx = c.transaction().await.unwrap();
        db::auth_lock(&tx, false).await.unwrap();
        let (value, status) = execute(&tx, &self.base.app, &self.ctx, "createSubscription", &HashMap::new(), &json!({"topic":self.topic,"name":Uuid::new_v4().to_string(),"start":start,"maximum_attempts":attempts}), &HeaderMap::new(), "").await.unwrap();
        assert_eq!(status, 201);
        tx.commit().await.unwrap();
        value["id"].as_str().unwrap().parse().unwrap()
    }
    async fn next(&self, sub: Uuid) -> Option<Value> {
        let mut c = self.base.app.db.connect().await.unwrap();
        let tx = c.transaction().await.unwrap();
        db::auth_lock(&tx, false).await.unwrap();
        materialize(&tx).await.unwrap();
        let delivery = lease(&tx, &self.base.app.auth, &self.ctx, sub)
            .await
            .unwrap();
        tx.commit().await.unwrap();
        delivery
    }
    async fn finish(&self, sub: Uuid, delivery: &Value, outcome: &str) -> crate::error::Result<()> {
        let mut c = self.base.app.db.connect().await.unwrap();
        let tx = c.transaction().await.unwrap();
        db::auth_lock(&tx, false).await.unwrap();
        ack(
            &tx,
            &self.base.app.auth,
            &self.ctx,
            sub,
            &ack_body(delivery, outcome),
        )
        .await?;
        tx.commit().await.unwrap();
        Ok(())
    }
}
fn ack_body(delivery: &Value, outcome: &str) -> Value {
    json!({"delivery_id":delivery["delivery_id"],"lease_token":delivery["lease_token"],"outcome":outcome})
}
fn delivery_id(delivery: &Value) -> Uuid {
    delivery["delivery_id"].as_str().unwrap().parse().unwrap()
}

async fn research_encode_delivery(
    message: crate::rpc::EventDelivery,
    limit: usize,
) -> (bool, String) {
    use axum::{body::Body, http::Request};
    use http_body_util::BodyExt;
    use prost::Message;
    let input = crate::rpc::SubscribeRequest::default().encode_to_vec();
    let mut framed = vec![0];
    framed.extend_from_slice(&(input.len() as u32).to_be_bytes());
    framed.extend_from_slice(&input);
    let request = Request::builder()
        .method("POST")
        .uri("/xelab.v1.HostService/Subscribe")
        .header("content-type", "application/grpc")
        .body(Body::from(framed))
        .unwrap();
    let service = tower::service_fn(move |_: tonic::Request<crate::rpc::SubscribeRequest>| {
        let item = message.clone();
        async move { Ok::<_, tonic::Status>(tonic::Response::new(tokio_stream::once(Ok(item)))) }
    });
    let mut grpc = tonic::server::Grpc::new(tonic::codec::ProstCodec::<
        crate::rpc::EventDelivery,
        crate::rpc::SubscribeRequest,
    >::default())
    .max_encoding_message_size(limit);
    let mut body = grpc.server_streaming(service, request).await.into_body();
    let first = body.frame().await.unwrap().unwrap();
    let had_data = first.is_data();
    let trailers = if had_data {
        body.frame()
            .await
            .unwrap()
            .unwrap()
            .into_trailers()
            .unwrap()
    } else {
        first.into_trailers().unwrap()
    };
    let status = trailers
        .get("grpc-status")
        .unwrap()
        .to_str()
        .unwrap()
        .to_owned();
    (had_data, status)
}

#[tokio::test]
async fn research_compact_event_normalization_can_exceed_host_stream_limit() {
    use axum::{body::Body, extract::ConnectInfo, http::Request};
    use prost::Message;
    use tower::ServiceExt;

    let mut f = EventFixture::new()
        .await
        .expect("isolated PostgreSQL is required");
    let app = std::sync::Arc::get_mut(&mut f.base.app).expect("fixture must be uniquely owned");
    let mut config = app.config.value.clone();
    config["resources"]["profile"] = json!("compact");
    app.config = crate::config::Config::from_value(config).unwrap();
    let rpc_limit = app.config.rpc_limit(16_777_216 + 65_536);
    assert_eq!(rpc_limit, 327_680);

    let c = f.base.app.db.connect().await.unwrap();
    let installation: Uuid = c
        .query_one(
            "SELECT id FROM installations WHERE plugin_id='org.xelab.backup-fixture'",
            &[],
        )
        .await
        .unwrap()
        .get(0);
    let schema = json!({"type":"array","items":{"type":"number"}});
    let digest = contracts::digest(&serde_json::to_vec(&schema).unwrap());
    let manifest = json!({"plugin_id":"org.xelab.backup-fixture","event_topics":[{"topic":f.topic,"version":"1.0.0","payload_schema":schema,"schema_digest":digest}]});
    validate_manifest(&manifest).unwrap();
    c.execute(
        "UPDATE installations SET manifest=$2 WHERE id=$1",
        &[&installation, &manifest],
    )
    .await
    .unwrap();
    c.execute(
        "UPDATE event_contracts SET schema_digest=$2,payload_schema=$3 WHERE topic=$1",
        &[&f.topic, &digest, &schema],
    )
    .await
    .unwrap();
    let subscription = f.subscription("latest", 1).await;

    let payload = format!("[{}]", vec!["9e9"; 60_000].join(","));
    let raw_body = format!("{{\"topic\":\"{}\",\"version\":\"1.0.0\",\"payload\":{},\"dedupe_key\":\"compact-fixture\"}}", f.topic, payload);
    assert!(raw_body.len() < 256 * 1024);
    let mut request = Request::builder()
        .method("POST")
        .uri("/api/platform/v1/events")
        .header(
            "host",
            crate::plugin_http_gateway::core_authority(&f.base.app.config).unwrap(),
        )
        .header("authorization", format!("Bearer {}", f.base.token))
        .header("idempotency-key", "compact-fixture")
        .header("content-type", "application/json")
        .body(Body::from(raw_body.clone()))
        .unwrap();
    request.extensions_mut().insert(ConnectInfo(
        "127.0.0.1:50400".parse::<std::net::SocketAddr>().unwrap(),
    ));
    let response = crate::api::router(f.base.app.clone())
        .oneshot(request)
        .await
        .unwrap();
    assert_eq!(
        response.status().as_u16(),
        201,
        "compact HTTP publication should be accepted"
    );
    drop(response);

    let leased = f
        .next(subscription)
        .await
        .expect("published event should be leased");
    let event = &leased["event"];
    let payload_json = serde_json::to_vec(&event["payload"]).unwrap();
    let message = crate::rpc::EventDelivery {
        delivery_id: leased["delivery_id"].as_str().unwrap().into(),
        event_id: event["id"].as_str().unwrap().into(),
        topic: event["topic"].as_str().unwrap().into(),
        version: event["version"].as_str().unwrap().into(),
        payload_json,
        attempt: leased["attempt"].as_u64().unwrap() as u32,
        lease_token: leased["lease_token"].as_str().unwrap().into(),
        lease_expires_unix_ms: chrono::DateTime::parse_from_rfc3339(
            leased["lease_expires_at"].as_str().unwrap(),
        )
        .unwrap()
        .timestamp_millis(),
    };
    let encoded_len = message.encoded_len();
    assert!(encoded_len > rpc_limit);
    let stored = c
        .query_one(
            "SELECT state,attempt FROM event_deliveries WHERE id=$1",
            &[&message.delivery_id.parse::<Uuid>().unwrap()],
        )
        .await
        .unwrap();
    assert_eq!(stored.get::<_, String>("state"), "leased");
    assert_eq!(stored.get::<_, i32>("attempt"), 1);

    // Exercise the same Tonic streaming encoder and cap as HostService. A
    // small control is delivered, while the committed large item fails first.
    let mut small = message.clone();
    small.payload_json = b"[1]".to_vec();
    assert_eq!(
        research_encode_delivery(small, rpc_limit).await,
        (true, "0".to_owned())
    );
    assert_eq!(
        research_encode_delivery(message, rpc_limit).await,
        (false, "11".to_owned())
    );

    // With one allowed attempt, wait for the real PostgreSQL lease timeout;
    // the next normal poll dead-letters it without any consumer receiving it.
    let started = tokio::time::Instant::now();
    loop {
        let expired: bool = c
            .query_one(
                "SELECT clock_timestamp() >= lease_expires_at FROM event_deliveries WHERE id=$1",
                &[&delivery_id(&leased)],
            )
            .await
            .unwrap()
            .get(0);
        if expired {
            break;
        }
        assert!(
            started.elapsed() < std::time::Duration::from_secs(45),
            "database lease did not expire within the bounded wait"
        );
        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
    }
    assert!(f.next(subscription).await.is_none());
    let final_row = c.query_one("SELECT d.state, d.outcome, (SELECT count(*) FROM event_dead_letters l WHERE l.delivery_id=d.id), (SELECT cursor FROM subscriptions WHERE id=$2)=d.event_id FROM event_deliveries d WHERE d.id=$1", &[&delivery_id(&leased), &subscription]).await.unwrap();
    assert_eq!(final_row.get::<_, String>(0), "dead_letter");
    assert_eq!(
        final_row.get::<_, Option<String>>(1).as_deref(),
        Some("dead_letter")
    );
    assert_eq!(final_row.get::<_, i64>(2), 1);
    assert!(final_row.get::<_, bool>(3));
    println!("http_request_bytes={}, host_grpc_message_bytes={}, host_grpc_limit={}, grpc_status=11, persisted_lease_attempt=1, dead_letters=1, cursor_advanced=true", raw_body.len(), encoded_len, rpc_limit);
}

#[tokio::test]
async fn actual_postgres_event_dedupe_is_durable_concurrent_and_schema_checked() {
    let Some(f) = EventFixture::new().await else {
        return;
    };
    let body = f.body("concurrent-publish", 7);
    let writer = || async {
        let mut c = f.base.app.db.connect().await.unwrap();
        let tx = c.transaction().await.unwrap();
        db::auth_lock(&tx, false).await.unwrap();
        let result = append(&tx, &f.base.app.auth, &f.ctx, &body).await.unwrap();
        tx.commit().await.unwrap();
        result
    };
    let (first, second) = tokio::join!(writer(), writer());
    assert_eq!(first.0["id"], second.0["id"]);
    assert_ne!(
        first.1, second.1,
        "exactly one publisher must create the event"
    );
    let mut c = f.base.app.db.connect().await.unwrap();
    let tx = c.transaction().await.unwrap();
    db::auth_lock(&tx, false).await.unwrap();
    let counts = tx
        .query_one(
            "SELECT (SELECT count(*) FROM events),(SELECT count(*) FROM event_dedupe)",
            &[],
        )
        .await
        .unwrap();
    assert_eq!(counts.get::<_, i64>(0), 1);
    assert_eq!(counts.get::<_, i64>(1), 1);
    assert_eq!(
        append(
            &tx,
            &f.base.app.auth,
            &f.ctx,
            &f.body("concurrent-publish", 8)
        )
        .await
        .unwrap_err()
        .code,
        "STATE_CONFLICT"
    );
    tx.rollback().await.unwrap();
    let tx = c.transaction().await.unwrap();
    db::auth_lock(&tx, false).await.unwrap();
    let mut bad = f.body("bad-schema", 1);
    bad["payload"] = json!({"value":"wrong type"});
    assert_eq!(
        append(&tx, &f.base.app.auth, &f.ctx, &bad)
            .await
            .unwrap_err()
            .code,
        "INVALID_ARGUMENT"
    );
    tx.rollback().await.unwrap();
    let tx = c.transaction().await.unwrap();
    db::auth_lock(&tx, false).await.unwrap();
    let mut missing = f.body("missing-contract", 1);
    missing["topic"] = json!("org.xelab.uninstalled.completed");
    assert!(append(&tx, &f.base.app.auth, &f.ctx, &missing)
        .await
        .is_err());
    tx.rollback().await.unwrap();
    let tx = c.transaction().await.unwrap();
    db::auth_lock(&tx, false).await.unwrap();
    append(
        &tx,
        &f.base.app.auth,
        &f.ctx,
        &f.body("lossless-large-integer", 9007199254740992),
    )
    .await
    .unwrap();
    tx.commit().await.unwrap();
    let tx = c.transaction().await.unwrap();
    db::auth_lock(&tx, false).await.unwrap();
    assert_eq!(
        append(
            &tx,
            &f.base.app.auth,
            &f.ctx,
            &f.body("lossless-large-integer", 9007199254740993)
        )
        .await
        .unwrap_err()
        .code,
        "STATE_CONFLICT",
        "dedupe must not round distinct large JSON integers into one payload"
    );
}

#[tokio::test]
async fn actual_postgres_event_single_lease_expiry_takeover_and_ack_fencing() {
    let Some(f) = EventFixture::new().await else {
        return;
    };
    let sub = f.subscription("earliest", 5).await;
    let event = f.publish("redelivery", 1).await;
    let first = f.next(sub).await.unwrap();
    assert_eq!(first["event"]["id"], event["id"]);
    assert_eq!(first["attempt"], 1);
    assert!(
        f.next(sub).await.is_none(),
        "a live lease must prevent another delivery"
    );
    let mut forged = first.clone();
    forged["lease_token"] = json!("forged-lease-token");
    assert_eq!(
        f.finish(sub, &forged, "ack").await.unwrap_err().code,
        "STATE_CONFLICT"
    );
    let c = f.base.app.db.connect().await.unwrap();
    c.execute(
        "UPDATE event_deliveries SET lease_expires_at=now()-interval '100 milliseconds' WHERE id=$1",
        &[&delivery_id(&first)],
    )
    .await
    .unwrap();
    assert_eq!(
        f.finish(sub, &first, "ack").await.unwrap_err().code,
        "STATE_CONFLICT",
        "expired lease is invalid before takeover too"
    );
    // A long process downtime may already cover the persisted backoff.
    let second = if let Some(delivery) = f.next(sub).await {
        delivery
    } else {
        c.execute(
            "UPDATE event_deliveries SET retry_at=now()-interval '1 second' WHERE id=$1",
            &[&delivery_id(&first)],
        )
        .await
        .unwrap();
        f.next(sub).await.unwrap()
    };
    assert_eq!(second["event"]["id"], event["id"]);
    assert_eq!(second["attempt"], 2);
    assert_ne!(first["delivery_id"], second["delivery_id"]);
    assert_ne!(first["lease_token"], second["lease_token"]);
    assert_eq!(
        f.finish(sub, &first, "ack").await.unwrap_err().code,
        "STATE_CONFLICT"
    );
    f.finish(sub, &second, "ack").await.unwrap();
    f.finish(sub, &second, "ack").await.unwrap();
    assert_eq!(
        f.finish(sub, &second, "retry").await.unwrap_err().code,
        "STATE_CONFLICT",
        "same lease cannot change its committed outcome"
    );
    let cursor: Option<Uuid> = c
        .query_one("SELECT cursor FROM subscriptions WHERE id=$1", &[&sub])
        .await
        .unwrap()
        .get(0);
    assert_eq!(cursor, Some(event["id"].as_str().unwrap().parse().unwrap()));
    assert!(f.next(sub).await.is_none());
}

#[tokio::test]
async fn actual_postgres_event_retry_exhaustion_and_explicit_dead_letter_preserve_evidence() {
    let Some(f) = EventFixture::new().await else {
        return;
    };
    let sub = f.subscription("earliest", 2).await;
    let event = f.publish("exhaustion", 1).await;
    let first = f.next(sub).await.unwrap();
    let mut retry_connection = f.base.app.db.connect().await.unwrap();
    let retry_tx = retry_connection.transaction().await.unwrap();
    db::auth_lock(&retry_tx, false).await.unwrap();
    let before: chrono::DateTime<chrono::Utc> = retry_tx
        .query_one("SELECT clock_timestamp()", &[])
        .await
        .unwrap()
        .get(0);
    ack(
        &retry_tx,
        &f.base.app.auth,
        &f.ctx,
        sub,
        &ack_body(&first, "retry"),
    )
    .await
    .unwrap();
    let retry_state = retry_tx
        .query_one(
            "SELECT retry_at,clock_timestamp() FROM event_deliveries WHERE id=$1",
            &[&delivery_id(&first)],
        )
        .await
        .unwrap();
    let retry_at: chrono::DateTime<chrono::Utc> = retry_state.get(0);
    let observed: chrono::DateTime<chrono::Utc> = retry_state.get(1);
    assert!(retry_at >= before + chrono::Duration::seconds(1));
    assert!(retry_at <= observed + chrono::Duration::milliseconds(1250));
    // Hold a deterministic future deadline while the current authority check
    // runs; elapsed wall time during that check must not make this assertion flaky.
    retry_tx
        .execute(
            "UPDATE event_deliveries SET retry_at=clock_timestamp()+interval '1 hour' WHERE id=$1",
            &[&delivery_id(&first)],
        )
        .await
        .unwrap();
    assert!(
        lease(&retry_tx, &f.base.app.auth, &f.ctx, sub)
            .await
            .unwrap()
            .is_none(),
        "a persisted future retry deadline must prevent early delivery"
    );
    retry_tx.commit().await.unwrap();
    let c = f.base.app.db.connect().await.unwrap();
    c.execute(
        "UPDATE event_deliveries SET retry_at=now()-interval '1 second' WHERE id=$1",
        &[&delivery_id(&first)],
    )
    .await
    .unwrap();
    let second = f.next(sub).await.unwrap();
    assert_eq!(second["attempt"], 2);
    f.finish(sub, &second, "retry").await.unwrap();
    let dead = c
        .query_one(
            "SELECT reason FROM event_dead_letters WHERE delivery_id=$1",
            &[&delivery_id(&second)],
        )
        .await
        .unwrap();
    assert_eq!(dead.get::<_, String>(0), "maximum_attempts");
    let cursor: Option<Uuid> = c
        .query_one("SELECT cursor FROM subscriptions WHERE id=$1", &[&sub])
        .await
        .unwrap()
        .get(0);
    assert_eq!(cursor, Some(event["id"].as_str().unwrap().parse().unwrap()));
    let next_event = f.publish("explicit-dead-letter", 2).await;
    let third = f.next(sub).await.unwrap();
    assert_eq!(third["event"]["id"], next_event["id"]);
    f.finish(sub, &third, "dead_letter").await.unwrap();
    assert!(f.next(sub).await.is_none());
    let reason: String = c
        .query_one(
            "SELECT reason FROM event_dead_letters WHERE delivery_id=$1",
            &[&delivery_id(&third)],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(reason, "consumer_dead_letter");
}

#[tokio::test]
async fn actual_postgres_event_start_watermarks_and_aborted_publish_survive_reconnect() {
    let Some(f) = EventFixture::new().await else {
        return;
    };
    let earlier = f.publish("before-create", 1).await;
    // latest must exclude even the accepted outbox whose materializer has not run.
    let latest = f.subscription("latest", 5).await;
    let earliest = f.subscription("earliest", 5).await;
    let after = f.publish("after-create", 2).await;
    let a = f.next(latest).await.unwrap();
    let b = f.next(earliest).await.unwrap();
    assert_eq!(a["event"]["id"], after["id"]);
    assert_eq!(b["event"]["id"], earlier["id"]);
    {
        let mut c = f.base.app.db.connect().await.unwrap();
        let tx = c.transaction().await.unwrap();
        db::auth_lock(&tx, false).await.unwrap();
        append(
            &tx,
            &f.base.app.auth,
            &f.ctx,
            &f.body("uncommitted-writer", 99),
        )
        .await
        .unwrap();
        // Dropping the transaction models a connection/handler crash before commit.
    }
    let c = f.base.app.db.connect().await.unwrap();
    assert_eq!(
        c.query_one(
            "SELECT count(*) FROM events WHERE dedupe_key='uncommitted-writer'",
            &[]
        )
        .await
        .unwrap()
        .get::<_, i64>(0),
        0
    );
    // Lose the response after committed append: a fresh connection dedupes to the
    // same event and materializing twice cannot create a second visible Event.
    let committed = f.publish("committed-response-lost", 3).await;
    let mut reconnected = f.base.app.db.connect().await.unwrap();
    let tx = reconnected.transaction().await.unwrap();
    db::auth_lock(&tx, false).await.unwrap();
    let (replayed, duplicate) = append(
        &tx,
        &f.base.app.auth,
        &f.ctx,
        &f.body("committed-response-lost", 3),
    )
    .await
    .unwrap();
    assert!(duplicate);
    assert_eq!(replayed["id"], committed["id"]);
    materialize(&tx).await.unwrap();
    materialize(&tx).await.unwrap();
    tx.commit().await.unwrap();
    assert_eq!(
        c.query_one(
            "SELECT count(*) FROM events WHERE dedupe_key='committed-response-lost'",
            &[]
        )
        .await
        .unwrap()
        .get::<_, i64>(0),
        1
    );
}

#[tokio::test]
async fn actual_postgres_event_disabled_and_revoked_current_consumer_stop_but_fresh_owner_continues(
) {
    let Some(f) = EventFixture::new().await else {
        return;
    };
    let sub = f.subscription("earliest", 5).await;
    f.publish("disabled-ack", 1).await;
    let delivery = f.next(sub).await.unwrap();
    let c = f.base.app.db.connect().await.unwrap();
    c.execute(
        "UPDATE subscriptions SET status='disabled' WHERE id=$1",
        &[&sub],
    )
    .await
    .unwrap();
    assert!(f.finish(sub, &delivery, "ack").await.is_err());
    let mut c = f.base.app.db.connect().await.unwrap();
    let tx = c.transaction().await.unwrap();
    db::auth_lock(&tx, false).await.unwrap();
    assert!(lease(&tx, &f.base.app.auth, &f.ctx, sub).await.is_err());
    tx.rollback().await.unwrap();
    c.execute(
        "UPDATE subscriptions SET status='active' WHERE id=$1",
        &[&sub],
    )
    .await
    .unwrap();
    c.execute(
        "UPDATE credentials SET status='revoked',revision=revision+1 WHERE id=$1",
        &[&f.ctx.credential],
    )
    .await
    .unwrap();
    assert!(f.finish(sub, &delivery, "ack").await.is_err());
    let tx = c.transaction().await.unwrap();
    db::auth_lock(&tx, false).await.unwrap();
    assert!(lease(&tx, &f.base.app.auth, &f.ctx, sub).await.is_err());
    tx.rollback().await.unwrap();
    let issued = f
        .base
        .app
        .auth
        .login(
            &c,
            f.ctx.subject,
            "correct horse backup fixture password".into(),
            "127.0.0.1".parse().unwrap(),
            Uuid::new_v4(),
        )
        .await
        .unwrap();
    let replacement = f
        .base
        .app
        .auth
        .authenticate(
            &c,
            issued["access_token"].as_str().unwrap(),
            "127.0.0.1".parse().unwrap(),
            Uuid::new_v4(),
        )
        .await
        .unwrap();
    let count: i64 = c
        .query_one(
            "SELECT count(*) FROM event_deliveries WHERE subscription_id=$1 AND state='leased'",
            &[&sub],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(
        count, 1,
        "revoked current consumer cannot falsely acknowledge work"
    );
    let tx = c.transaction().await.unwrap();
    db::auth_lock(&tx, false).await.unwrap();
    assert!(
        lease(&tx, &f.base.app.auth, &replacement, sub)
            .await
            .unwrap()
            .is_none(),
        "fresh authorized owner retains the same outstanding lease"
    );
    ack(
        &tx,
        &f.base.app.auth,
        &replacement,
        sub,
        &ack_body(&delivery, "ack"),
    )
    .await
    .unwrap();
    let (next_event, _) = append(
        &tx,
        &f.base.app.auth,
        &replacement,
        &f.body("fresh-current-owner", 2),
    )
    .await
    .unwrap();
    materialize(&tx).await.unwrap();
    let next_delivery = lease(&tx, &f.base.app.auth, &replacement, sub)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        next_delivery["event"]["id"], next_event["id"],
        "durable cursor survives expiry/revocation of the creator session"
    );
    tx.commit().await.unwrap();
}

#[tokio::test]
async fn actual_postgres_event_quota_and_retention_never_drop_active_unacknowledged_payloads() {
    let Some(f) = EventFixture::new().await else {
        return;
    };
    let sub = f.subscription("earliest", 5).await;
    let event = f.publish("retained-unacked", 1).await;
    let delivery = f.next(sub).await.unwrap();
    let mut c = f.base.app.db.connect().await.unwrap();
    c.execute(
        "UPDATE events SET expires_at=now()-interval '1 second' WHERE id=$1",
        &[&event["id"].as_str().unwrap().parse::<Uuid>().unwrap()],
    )
    .await
    .unwrap();
    {
        let tx = c.transaction().await.unwrap();
        maintenance(&tx).await.unwrap();
        tx.commit().await.unwrap();
    }
    assert_eq!(
        c.query_one(
            "SELECT count(*) FROM events WHERE id=$1",
            &[&event["id"].as_str().unwrap().parse::<Uuid>().unwrap()]
        )
        .await
        .unwrap()
        .get::<_, i64>(0),
        1
    );
    c.execute("UPDATE event_store_limits SET maximum_bytes=1", &[])
        .await
        .unwrap();
    let tx = c.transaction().await.unwrap();
    db::auth_lock(&tx, false).await.unwrap();
    assert_eq!(
        append(&tx, &f.base.app.auth, &f.ctx, &f.body("quota-rejected", 2))
            .await
            .unwrap_err()
            .code,
        "RESOURCE_EXHAUSTED"
    );
    tx.rollback().await.unwrap();
    assert_eq!(
        c.query_one(
            "SELECT count(*) FROM events WHERE dedupe_key='quota-rejected'",
            &[]
        )
        .await
        .unwrap()
        .get::<_, i64>(0),
        0
    );
    f.finish(sub, &delivery, "dead_letter").await.unwrap();
    // Dead-letter evidence and payload must survive normal seven-day event TTL.
    let tx = c.transaction().await.unwrap();
    maintenance(&tx).await.unwrap();
    tx.commit().await.unwrap();
    assert_eq!(
        c.query_one(
            "SELECT count(*) FROM event_dead_letters WHERE delivery_id=$1",
            &[&delivery_id(&delivery)]
        )
        .await
        .unwrap()
        .get::<_, i64>(0),
        1
    );
    assert_eq!(
        c.query_one(
            "SELECT count(*) FROM events WHERE id=$1",
            &[&event["id"].as_str().unwrap().parse::<Uuid>().unwrap()]
        )
        .await
        .unwrap()
        .get::<_, i64>(0),
        1
    );
}

async fn public_request(
    f: &EventFixture,
    method: &str,
    path: &str,
    body: Value,
    key: &str,
    revision: Option<i64>,
) -> (u16, Value) {
    use axum::{
        body::{to_bytes, Body},
        extract::ConnectInfo,
        http::Request,
    };
    use tower::ServiceExt;
    let mut request = Request::builder()
        .method(method)
        .uri(path)
        .header(
            "host",
            crate::plugin_http_gateway::core_authority(&f.base.app.config).unwrap(),
        )
        .header("authorization", format!("Bearer {}", f.base.token))
        .header("idempotency-key", key);
    if let Some(revision) = revision {
        request = request.header("if-match", format!("\"rev-{revision}\""));
    }
    let bytes = if body.is_null() {
        Vec::new()
    } else {
        request = request.header("content-type", "application/json");
        serde_json::to_vec(&body).unwrap()
    };
    let mut request = request.body(Body::from(bytes)).unwrap();
    request.extensions_mut().insert(ConnectInfo(
        "127.0.0.1:50400".parse::<std::net::SocketAddr>().unwrap(),
    ));
    let response = crate::api::router(f.base.app.clone())
        .oneshot(request)
        .await
        .unwrap();
    let status = response.status().as_u16();
    let bytes = to_bytes(response.into_body(), 4194304).await.unwrap();
    (
        status,
        if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap()
        },
    )
}

#[tokio::test]
async fn actual_postgres_public_event_api_idempotency_etag_and_subscription_ownership() {
    let Some(f) = EventFixture::new().await else {
        return;
    };
    let body = f.body("public-dedupe", 7);
    let first = public_request(
        &f,
        "POST",
        "/api/platform/v1/events",
        body.clone(),
        "event-publish",
        None,
    )
    .await;
    assert_eq!(first.0, 201, "{}", first.1);
    let second = public_request(
        &f,
        "POST",
        "/api/platform/v1/events",
        body.clone(),
        "event-publish",
        None,
    )
    .await;
    assert_eq!(second.0, 201, "{}", second.1);
    assert_eq!(first.1["id"], second.1["id"]);
    let changed = public_request(
        &f,
        "POST",
        "/api/platform/v1/events",
        f.body("public-dedupe", 8),
        "event-publish",
        None,
    )
    .await;
    assert_eq!(changed.0, 409, "{}", changed.1);
    let sub_body = json!({"topic":f.topic,"name":"public-subscription","start":"earliest","maximum_attempts":5});
    let sub = public_request(
        &f,
        "POST",
        "/api/platform/v1/subscriptions",
        sub_body.clone(),
        "event-subscribe",
        None,
    )
    .await;
    assert_eq!(sub.0, 201, "{}", sub.1);
    let retry = public_request(
        &f,
        "POST",
        "/api/platform/v1/subscriptions",
        sub_body,
        "event-subscribe",
        None,
    )
    .await;
    assert_eq!(retry.0, 201);
    assert_eq!(retry.1["id"], sub.1["id"]);
    let id = sub.1["id"].as_str().unwrap();
    let path = format!("/api/platform/v1/subscriptions/{id}");
    let read = public_request(&f, "GET", &path, Value::Null, "unused", None).await;
    assert_eq!(read.0, 200, "{}", read.1);
    let stale = public_request(
        &f,
        "DELETE",
        &path,
        Value::Null,
        "event-delete-stale",
        Some(read.1["revision"].as_i64().unwrap() + 1),
    )
    .await;
    assert_eq!(stale.0, 412, "{}", stale.1);
    let deleted = public_request(
        &f,
        "DELETE",
        &path,
        Value::Null,
        "event-delete",
        Some(read.1["revision"].as_i64().unwrap()),
    )
    .await;
    assert_eq!(deleted.0, 204, "{}", deleted.1);
    let c = f.base.app.db.connect().await.unwrap();
    let status: String = c
        .query_one(
            "SELECT status FROM subscriptions WHERE id=$1",
            &[&id.parse::<Uuid>().unwrap()],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(status, "disabled");
}

#[tokio::test]
async fn actual_postgres_event_concurrent_consumers_cannot_create_two_live_leases() {
    let Some(f) = EventFixture::new().await else {
        return;
    };
    let sub = f.subscription("earliest", 5).await;
    f.publish("race-consumers", 1).await;
    let (a, b) = tokio::join!(f.next(sub), f.next(sub));
    assert_ne!(
        a.is_some(),
        b.is_some(),
        "exactly one consumer wins a current lease"
    );
    let c = f.base.app.db.connect().await.unwrap();
    let count: i64 = c
        .query_one(
            "SELECT count(*) FROM event_deliveries WHERE subscription_id=$1 AND state='leased'",
            &[&sub],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(count, 1);
}

#[tokio::test]
async fn actual_postgres_event_sigkill_and_reexec_preserve_delivery_and_fence_old_lease() {
    use std::os::unix::process::ExitStatusExt;
    let Some(f) = EventFixture::new().await else {
        return;
    };
    let sub = f.subscription("earliest", 5).await;
    let c = f.base.app.db.connect().await.unwrap();
    let database: String = c
        .query_one("SELECT current_database()", &[])
        .await
        .unwrap()
        .get(0);
    let dsn = format!(
        "{} dbname={database}",
        std::env::var("XELAB_TEST_DATABASE_URL").unwrap()
    );
    let directory = tempfile::tempdir().unwrap();
    let file = directory.path().join("committed-delivery.json");
    let helper = format!(
        "{}::actual_postgres_event_process_helper",
        module_path!().split_once("::").unwrap().1
    );
    let make_child = |mode: &str| {
        let mut command = tokio::process::Command::new(std::env::current_exe().unwrap());
        command
            .args(["--exact", &helper, "--ignored", "--nocapture"])
            .env("XELAB_EVENT_CHILD_MODE", mode)
            .env("XELAB_EVENT_CHILD_DSN", &dsn)
            .env("XELAB_EVENT_CHILD_TOKEN", &f.base.token)
            .env("XELAB_EVENT_CHILD_SUB", sub.to_string())
            .env("XELAB_EVENT_CHILD_PATH", &file)
            .kill_on_drop(true);
        command
    };
    let mut writer = make_child("writer").spawn().unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(20), async {
        while !file.exists() {
            assert!(
                writer.try_wait().unwrap().is_none(),
                "event child exited before its committed marker"
            );
            tokio::time::sleep(std::time::Duration::from_millis(25)).await;
        }
    })
    .await
    .expect("event writer did not commit within its bounded deadline");
    let first: Value = serde_json::from_slice(&std::fs::read(&file).unwrap()).unwrap();
    assert_eq!(first["attempt"], 1);
    writer.start_kill().unwrap();
    let exit = writer.wait().await.unwrap();
    assert_eq!(exit.signal(), Some(libc::SIGKILL));
    c.execute(
        "UPDATE event_deliveries SET lease_expires_at=now()-interval '100 milliseconds' WHERE id=$1",
        &[&delivery_id(&first)],
    )
    .await
    .unwrap();
    let recovery = make_child("recover").status().await.unwrap();
    assert!(recovery.success());
    let second: Value = serde_json::from_slice(&std::fs::read(&file).unwrap()).unwrap();
    assert_eq!(second["event"]["id"], first["event"]["id"]);
    assert_eq!(second["attempt"], 2);
    assert_ne!(second["lease_token"], first["lease_token"]);
    assert_eq!(
        f.finish(sub, &first, "ack").await.unwrap_err().code,
        "STATE_CONFLICT"
    );
    f.finish(sub, &second, "ack").await.unwrap();
}

#[tokio::test]
#[ignore = "private helper launched only by the event SIGKILL/reexec regression"]
async fn actual_postgres_event_process_helper() {
    use std::os::unix::fs::OpenOptionsExt;
    let mode = std::env::var("XELAB_EVENT_CHILD_MODE").expect("event child mode required");
    let db = crate::db::Db::new(std::env::var("XELAB_EVENT_CHILD_DSN").unwrap()).unwrap();
    let auth = crate::auth::Auth::new(vec![69; 32]).unwrap();
    let mut c = db.connect().await.unwrap();
    let token = std::env::var("XELAB_EVENT_CHILD_TOKEN").unwrap();
    let ctx = auth
        .authenticate(&c, &token, "127.0.0.1".parse().unwrap(), Uuid::new_v4())
        .await
        .unwrap();
    let sub: Uuid = std::env::var("XELAB_EVENT_CHILD_SUB")
        .unwrap()
        .parse()
        .unwrap();
    let path = std::path::PathBuf::from(std::env::var("XELAB_EVENT_CHILD_PATH").unwrap());
    let delivery = if mode == "writer" {
        let tx = c.transaction().await.unwrap();
        db::auth_lock(&tx, false).await.unwrap();
        append(&tx,&auth,&ctx,&json!({"topic":"org.xelab.backup-fixture.completed","version":"1.0.0","payload":{"value":42},"dedupe_key":"killed-writer-committed"})).await.unwrap();
        materialize(&tx).await.unwrap();
        let delivery = lease(&tx, &auth, &ctx, sub).await.unwrap().unwrap();
        tx.commit().await.unwrap();
        delivery
    } else {
        assert_eq!(mode, "recover");
        let old: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        let tx = c.transaction().await.unwrap();
        db::auth_lock(&tx, false).await.unwrap();
        assert_eq!(
            ack(&tx, &auth, &ctx, sub, &ack_body(&old, "ack"))
                .await
                .unwrap_err()
                .code,
            "STATE_CONFLICT"
        );
        let delivery = if let Some(delivery) = lease(&tx, &auth, &ctx, sub).await.unwrap() {
            delivery
        } else {
            tx.execute(
                "UPDATE event_deliveries SET retry_at=now()-interval '1 second' WHERE id=$1",
                &[&delivery_id(&old)],
            )
            .await
            .unwrap();
            lease(&tx, &auth, &ctx, sub).await.unwrap().unwrap()
        };
        tx.commit().await.unwrap();
        delivery
    };
    let temporary = path.with_extension("pending");
    let mut output = std::fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .mode(0o600)
        .open(&temporary)
        .unwrap();
    std::io::Write::write_all(&mut output, &serde_json::to_vec(&delivery).unwrap()).unwrap();
    output.sync_all().unwrap();
    std::fs::rename(temporary, &path).unwrap();
    if mode == "writer" {
        std::future::pending::<()>().await;
    }
}

async fn public_stream(f: &EventFixture, sub: Uuid) -> axum::response::Response {
    use axum::{body::Body, extract::ConnectInfo, http::Request};
    use tower::ServiceExt;
    let mut request = Request::builder()
        .method("GET")
        .uri(format!("/api/platform/v1/subscriptions/{sub}/deliveries"))
        .header(
            "host",
            crate::plugin_http_gateway::core_authority(&f.base.app.config).unwrap(),
        )
        .header("authorization", format!("Bearer {}", f.base.token))
        .body(Body::empty())
        .unwrap();
    request.extensions_mut().insert(ConnectInfo(
        "127.0.0.1:50400".parse::<std::net::SocketAddr>().unwrap(),
    ));
    crate::api::router(f.base.app.clone())
        .oneshot(request)
        .await
        .unwrap()
}

#[tokio::test]
async fn actual_postgres_event_sse_rechecks_revocation_before_first_body_poll() {
    use futures_util::StreamExt;
    let Some(f) = EventFixture::new().await else {
        return;
    };
    let sub = f.subscription("earliest", 5).await;
    f.publish("sse-revoke-before-poll", 1).await;
    let mut c = f.base.app.db.connect().await.unwrap();
    let tx = c.transaction().await.unwrap();
    materialize(&tx).await.unwrap();
    tx.commit().await.unwrap();
    let response = public_stream(&f, sub).await;
    assert_eq!(response.status().as_u16(), 200);
    let before: i64 = c
        .query_one(
            "SELECT count(*) FROM event_deliveries WHERE subscription_id=$1",
            &[&sub],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(
        before, 0,
        "an unpolled response must not lease unseen payloads"
    );
    c.execute(
        "UPDATE credentials SET status='revoked' WHERE id=$1",
        &[&f.ctx.credential],
    )
    .await
    .unwrap();
    let mut stream = response.into_body().into_data_stream();
    let frame = tokio::time::timeout(std::time::Duration::from_secs(15), stream.next())
        .await
        .unwrap();
    assert!(
        frame.is_none(),
        "no event payload may cross a revoked source"
    );
    let after: i64 = c
        .query_one(
            "SELECT count(*) FROM event_deliveries WHERE subscription_id=$1",
            &[&sub],
        )
        .await
        .unwrap()
        .get(0);
    assert_eq!(after, 0);
}

#[tokio::test]
async fn actual_postgres_event_sse_disconnect_retains_unacked_lease_and_cursor() {
    use futures_util::StreamExt;
    let Some(f) = EventFixture::new().await else {
        return;
    };
    let sub = f.subscription("earliest", 5).await;
    let event = f.publish("sse-drop-unacked", 1).await;
    let mut c = f.base.app.db.connect().await.unwrap();
    let tx = c.transaction().await.unwrap();
    materialize(&tx).await.unwrap();
    tx.commit().await.unwrap();
    let response = public_stream(&f, sub).await;
    assert_eq!(response.status().as_u16(), 200);
    assert_eq!(response.headers()["content-type"], "text/event-stream");
    let mut stream = response.into_body().into_data_stream();
    let frame = tokio::time::timeout(std::time::Duration::from_secs(15), stream.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let frame = std::str::from_utf8(&frame).unwrap();
    let data = frame
        .lines()
        .find_map(|line| line.strip_prefix("data: "))
        .expect("SSE Delivery JSON");
    let delivery: Value = serde_json::from_str(data).unwrap();
    assert_eq!(delivery["event"]["id"], event["id"]);
    contracts::validate("Delivery", &delivery).unwrap();
    drop(stream);
    let state=c.query_one("SELECT s.cursor,d.state FROM subscriptions s JOIN event_deliveries d ON d.subscription_id=s.id WHERE s.id=$1",&[&sub]).await.unwrap();
    assert_eq!(state.get::<_, Option<Uuid>>(0), None);
    assert_eq!(state.get::<_, String>(1), "leased");
    f.finish(sub, &delivery, "ack").await.unwrap();
}

#[tokio::test]
async fn actual_postgres_event_ack_idempotency_replay_rechecks_current_consumer_source() {
    let Some(mut f) = EventFixture::new().await else {
        return;
    };
    let sub = f.subscription("earliest", 5).await;
    f.publish("ack-replay-after-revocation", 1).await;
    let delivery = f.next(sub).await.unwrap();
    let c = f.base.app.db.connect().await.unwrap();
    let issued = f
        .base
        .app
        .auth
        .login(
            &c,
            f.ctx.subject,
            "correct horse backup fixture password".into(),
            "127.0.0.1".parse().unwrap(),
            Uuid::new_v4(),
        )
        .await
        .unwrap();
    f.base.token = issued["access_token"].as_str().unwrap().to_owned();
    let consumer: Uuid = issued["session"]["id"].as_str().unwrap().parse().unwrap();
    c.execute(
        "UPDATE credentials SET status='revoked',revision=revision+1 WHERE id=$1",
        &[&f.ctx.credential],
    )
    .await
    .unwrap();
    let path = format!("/api/platform/v1/subscriptions/{sub}/acks");
    let body = ack_body(&delivery, "ack");
    let accepted = public_request(
        &f,
        "POST",
        &path,
        body.clone(),
        "consumer-ack-before-revoke",
        None,
    )
    .await;
    assert_eq!(accepted.0, 204, "{}", accepted.1);
    c.execute(
        "UPDATE credentials SET status='revoked',revision=revision+1 WHERE id=$1",
        &[&consumer],
    )
    .await
    .unwrap();
    let replay = public_request(&f, "POST", &path, body, "consumer-ack-before-revoke", None).await;
    assert!(
        [401, 403, 409].contains(&replay.0),
        "ACK replay must recheck the revoked current consumer source: {} {}",
        replay.0,
        replay.1
    );
}

#[tokio::test]
async fn actual_postgres_event_upgrade_preserves_namespace_publisher_and_revocation_on_dedupe() {
    let Some(f) = EventFixture::new().await else {
        return;
    };
    let original = f.publish("old-installation", 1).await;
    let mut c = f.base.app.db.connect().await.unwrap();
    let row=c.query_one("SELECT id,publisher_id FROM installations WHERE plugin_id='org.xelab.backup-fixture' AND version='1.0.0'",&[]).await.unwrap();
    let old_installation: Uuid = row.get("id");
    let publisher: Uuid = row.get("publisher_id");
    let upgraded = Uuid::new_v4();
    c.execute("INSERT INTO installations(id,plugin_id,version,publisher_id,image_digest,manifest_digest,manifest,state,created_at,updated_at) SELECT $1,plugin_id,'2.0.0',publisher_id,image_digest,manifest_digest,manifest || '{\"version\":\"2.0.0\"}'::jsonb,'installed',now(),now() FROM installations WHERE id=$2",&[&upgraded,&old_installation]).await.unwrap();
    c.execute(
        "UPDATE installations SET state='uninstalled' WHERE id=$1",
        &[&old_installation],
    )
    .await
    .unwrap();
    let after = f.publish("after-upgrade", 2).await;
    assert_ne!(original["id"], after["id"]);
    let anchor:Uuid=c.query_one("SELECT publisher_installation_id FROM event_contracts WHERE topic=$1 AND version='1.0.0'",&[&f.topic]).await.unwrap().get(0);
    assert_eq!(
        anchor, old_installation,
        "an upgrade cannot replace the immutable namespace publisher anchor"
    );
    let public_body = f.body("public-before-publisher-revoke", 3);
    let public = public_request(
        &f,
        "POST",
        "/api/platform/v1/events",
        public_body.clone(),
        "publisher-trust-replay",
        None,
    )
    .await;
    assert_eq!(public.0, 201, "{}", public.1);
    c.execute(
        "UPDATE publishers SET status='revoked',revision=revision+1 WHERE id=$1",
        &[&publisher],
    )
    .await
    .unwrap();
    let replay = public_request(
        &f,
        "POST",
        "/api/platform/v1/events",
        public_body,
        "publisher-trust-replay",
        None,
    )
    .await;
    assert!(
        [401, 403, 404, 409].contains(&replay.0),
        "published Event replay must recheck publisher trust: {} {}",
        replay.0,
        replay.1
    );
    let tx = c.transaction().await.unwrap();
    db::auth_lock(&tx, false).await.unwrap();
    assert!(
        append(&tx, &f.base.app.auth, &f.ctx, &f.body("after-upgrade", 2))
            .await
            .is_err(),
        "even a dedupe replay must recheck namespace publisher trust"
    );
    tx.rollback().await.unwrap();
    let tx = c.transaction().await.unwrap();
    db::auth_lock(&tx, false).await.unwrap();
    assert!(
        append(&tx, &f.base.app.auth, &f.ctx, &f.body("after-revoke", 3))
            .await
            .is_err()
    );
}

#[tokio::test]
async fn actual_postgres_event_payload_fingerprint_matches_jsonb_normalization_and_snapshot_accounting(
) {
    let Some(f) = EventFixture::new().await else {
        return;
    };
    let mut c = f.base.app.db.connect().await.unwrap();
    let mut negative = f.body("signed-zero-normalized", 0);
    negative["payload"]["value"] = json!(-0.0f64);
    let tx = c.transaction().await.unwrap();
    db::auth_lock(&tx, false).await.unwrap();
    let (first, duplicate) = append(&tx, &f.base.app.auth, &f.ctx, &negative)
        .await
        .unwrap();
    assert!(!duplicate);
    tx.commit().await.unwrap();
    let mut positive = negative;
    positive["payload"]["value"] = json!(0.0f64);
    let tx = c.transaction().await.unwrap();
    db::auth_lock(&tx, false).await.unwrap();
    let (again, duplicate) = append(&tx, &f.base.app.auth, &f.ctx, &positive)
        .await
        .unwrap();
    assert!(
        duplicate,
        "JSONB-normalized signed zeros represent one payload"
    );
    assert_eq!(first["id"], again["id"]);
    let mut exponent = f.body("exponent-normalized", 0);
    exponent["payload"]["value"] = json!(1e18f64);
    let (event, duplicate) = append(&tx, &f.base.app.auth, &f.ctx, &exponent)
        .await
        .unwrap();
    assert!(!duplicate);
    tx.commit().await.unwrap();
    let row=c.query_one("SELECT e.payload,e.storage_bytes,d.payload_digest FROM events e JOIN event_dedupe d ON d.event_id=e.id WHERE e.id=$1",&[&event["id"].as_str().unwrap().parse::<Uuid>().unwrap()]).await.unwrap();
    let stored: Value = row.get("payload");
    assert_eq!(event["payload"], stored);
    let encoded = serde_json::to_vec(&stored).unwrap();
    assert_eq!(
        row.get::<_, i64>("storage_bytes"),
        (encoded.len() + 1024) as i64
    );
    assert_eq!(
        row.get::<_, String>("payload_digest"),
        contracts::digest(&encoded)
    );
    snapshot::validate_snapshot(&c).await.unwrap();
    let schema = json!({"type":"integer","maximum":9007199254740993u64});
    let manifest = json!({"plugin_id":"org.xelab.backup-fixture","event_topics":[{"topic":"org.xelab.backup-fixture.unsafe-schema","version":"1.0.0","payload_schema":schema,"schema_digest":contracts::digest(&serde_json::to_vec(&schema).unwrap())}]});
    let rejected = validate_manifest(&manifest).unwrap_err();
    assert_eq!(rejected.code, "INVALID_ARGUMENT");
    assert!(
        rejected.message.contains("I-JSON"),
        "ambiguous numeric schema digest must fail before trust: {rejected}"
    );
}

#[tokio::test]
async fn actual_postgres_event_new_earliest_subscription_does_not_pin_expired_unseen_events() {
    let Some(f) = EventFixture::new().await else {
        return;
    };
    let expired = f.publish("expired-before-subscription", 1).await;
    let id: Uuid = expired["id"].as_str().unwrap().parse().unwrap();
    let mut c = f.base.app.db.connect().await.unwrap();
    c.execute(
        "UPDATE events SET expires_at=now()-interval '1 day' WHERE id=$1",
        &[&id],
    )
    .await
    .unwrap();
    c.execute(
        "UPDATE event_dedupe SET expires_at=now()-interval '1 day' WHERE event_id=$1",
        &[&id],
    )
    .await
    .unwrap();
    let sub = f.subscription("earliest", 5).await;
    let tx = c.transaction().await.unwrap();
    db::auth_lock(&tx, false).await.unwrap();
    maintenance(&tx).await.unwrap();
    assert!(lease(&tx, &f.base.app.auth, &f.ctx, sub)
        .await
        .unwrap()
        .is_none());
    tx.commit().await.unwrap();
    let counts=c.query_one("SELECT (SELECT count(*) FROM events WHERE id=$1),(SELECT count(*) FROM event_dedupe WHERE event_id=$1),(SELECT count(*) FROM outbox WHERE topic='kernel.event.publish' AND aggregate_id=$1)",&[&id]).await.unwrap();
    assert_eq!(
        counts.get::<_, i64>(0),
        0,
        "fresh earliest subscriptions cannot keep an already expired Event alive"
    );
    assert_eq!(counts.get::<_, i64>(1), 0);
    assert_eq!(counts.get::<_, i64>(2), 0);
}

async fn selected_event_key(
    c: &(impl tokio_postgres::GenericClient + Sync),
    f: &EventFixture,
    account: Uuid,
    rules: Value,
) -> crate::auth::Context {
    let params = HashMap::from([("account_id".to_owned(), account.to_string())]);
    let (issued,status)=crate::identity::execute(c,&f.base.app.auth,&f.ctx,"createApiKey",&params,&json!({"name":"event scope regression","inheritance":{"mode":"selected","rules":rules},"conditions":{"expires_at":chrono::Utc::now()+chrono::Duration::hours(1)}}),&HeaderMap::new(),"").await.unwrap();
    assert_eq!(status, 201);
    f.base
        .app
        .auth
        .authenticate(
            c,
            issued["secret"].as_str().unwrap(),
            f.ctx.peer,
            Uuid::new_v4(),
        )
        .await
        .unwrap()
}

#[tokio::test]
async fn actual_postgres_event_global_topics_require_explicit_scope_and_subscription_owner() {
    let Some(f) = EventFixture::new().await else {
        return;
    };
    let own = f.subscription("earliest", 5).await;
    f.publish("global-topic-scope", 1).await;
    let mut c = f.base.app.db.connect().await.unwrap();
    let setup = c.transaction().await.unwrap();
    db::auth_lock(&setup, true).await.unwrap();
    let owned_rules = json!([
        {"action":"event.subscribe","resource":{"kind":"event","match":"owned_by","owner_id":f.ctx.subject}},
        {"action":"event.publish","resource":{"kind":"event","match":"owned_by","owner_id":f.ctx.subject}}
    ]);
    let owned = selected_event_key(&setup, &f, f.ctx.subject, owned_rules).await;
    let exact_rules = json!([
        {"action":"event.subscribe","resource":{"kind":"event","match":"ids","ids":[f.topic]}},
        {"action":"event.publish","resource":{"kind":"event","match":"ids","ids":[f.topic]}}
    ]);
    let exact = selected_event_key(&setup, &f, f.ctx.subject, exact_rules).await;
    let subscribe_rule = json!({"action":"event.subscribe","resource":{"kind":"event","match":"ids","ids":[f.topic]}});
    let params = HashMap::from([("account_id".to_owned(), f.ctx.subject.to_string())]);
    let (child,status)=crate::identity::execute(&setup,&f.base.app.auth,&f.ctx,"createChildAccount",&params,&json!({"display_name":"independent event subscription owner","kind":"service","inheritance":{"mode":"selected","rules":[subscribe_rule]}}),&HeaderMap::new(),"").await.unwrap();
    assert_eq!(status, 201);
    let child_id: Uuid = child["account"]["id"].as_str().unwrap().parse().unwrap();
    let other = selected_event_key(&setup, &f, child_id, json!([subscribe_rule])).await;
    let (other_subscription,status)=execute(&setup,&f.base.app,&other,"createSubscription",&HashMap::new(),&json!({"topic":f.topic,"name":"other legitimate owner","start":"earliest","maximum_attempts":5}),&HeaderMap::new(),"").await.unwrap();
    assert_eq!(status, 201);
    let other_id: Uuid = other_subscription["id"].as_str().unwrap().parse().unwrap();
    setup.commit().await.unwrap();
    let delivery = f.next(own).await.unwrap();
    let tx = c.transaction().await.unwrap();
    db::auth_lock(&tx, false).await.unwrap();
    let own_params = HashMap::from([("subscription_id".to_owned(), own.to_string())]);
    assert_eq!(
        execute(
            &tx,
            &f.base.app,
            &owned,
            "getSubscription",
            &own_params,
            &Value::Null,
            &HeaderMap::new(),
            ""
        )
        .await
        .unwrap_err()
        .code,
        "PERMISSION_DENIED",
        "owning a subscription does not own its global topic"
    );
    assert_eq!(
        lease(&tx, &f.base.app.auth, &owned, own)
            .await
            .unwrap_err()
            .code,
        "PERMISSION_DENIED"
    );
    assert_eq!(
        ack(
            &tx,
            &f.base.app.auth,
            &owned,
            own,
            &ack_body(&delivery, "ack")
        )
        .await
        .unwrap_err()
        .code,
        "PERMISSION_DENIED"
    );
    assert_eq!(
        append(
            &tx,
            &f.base.app.auth,
            &owned,
            &f.body("owned-by-is-not-topic-authority", 2)
        )
        .await
        .unwrap_err()
        .code,
        "PERMISSION_DENIED"
    );
    let (_, status) = execute(
        &tx,
        &f.base.app,
        &exact,
        "getSubscription",
        &own_params,
        &Value::Null,
        &HeaderMap::new(),
        "",
    )
    .await
    .unwrap();
    assert_eq!(status, 200);
    assert!(lease(&tx, &f.base.app.auth, &exact, own)
        .await
        .unwrap()
        .is_none());
    ack(
        &tx,
        &f.base.app.auth,
        &exact,
        own,
        &ack_body(&delivery, "ack"),
    )
    .await
    .unwrap();
    append(
        &tx,
        &f.base.app.auth,
        &exact,
        &f.body("explicit-topic-authority", 3),
    )
    .await
    .unwrap();
    let other_params = HashMap::from([("subscription_id".to_owned(), other_id.to_string())]);
    assert_eq!(
        execute(
            &tx,
            &f.base.app,
            &exact,
            "getSubscription",
            &other_params,
            &Value::Null,
            &HeaderMap::new(),
            ""
        )
        .await
        .unwrap_err()
        .code,
        "RESOURCE_NOT_FOUND"
    );
    assert_eq!(
        lease(&tx, &f.base.app.auth, &exact, other_id)
            .await
            .unwrap_err()
            .code,
        "RESOURCE_NOT_FOUND"
    );
    let child_delivery = lease(&tx, &f.base.app.auth, &other, other_id)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        ack(
            &tx,
            &f.base.app.auth,
            &exact,
            other_id,
            &ack_body(&child_delivery, "ack")
        )
        .await
        .unwrap_err()
        .code,
        "RESOURCE_NOT_FOUND"
    );
    ack(
        &tx,
        &f.base.app.auth,
        &other,
        other_id,
        &ack_body(&child_delivery, "ack"),
    )
    .await
    .unwrap();
    tx.commit().await.unwrap();
}

#[test]
fn event_retry_schedule_contract_reaches_sixty_second_ceiling() {
    for attempt in 1..=6 {
        let base = 1i64 << (attempt - 1);
        let millis = backoff(attempt).num_milliseconds();
        assert!(
            (base * 1000..=base * 1250).contains(&millis),
            "attempt {attempt} backoff outside exponential+jitter bounds: {millis}ms"
        );
    }
    for attempt in 7..=10 {
        assert_eq!(
            backoff(attempt).num_milliseconds(),
            60000,
            "attempt {attempt} must use the maximum sixty-second delay"
        );
    }
}

#[tokio::test]
async fn actual_postgres_event_expired_publisher_waiting_for_storage_fence_cannot_commit() {
    let Some(f) = EventFixture::new().await else {
        return;
    };
    // Initialize limits and warm schema compilation before the bounded expiry.
    f.publish("storage-fence-warmup", 1).await;
    let mut blocker = f.base.app.db.connect().await.unwrap();
    let blocker_pid: i32 = blocker
        .query_one("SELECT pg_backend_pid()", &[])
        .await
        .unwrap()
        .get(0);
    let admin = f.base.app.db.connect().await.unwrap();
    let expiry:chrono::DateTime<chrono::Utc>=admin.query_one("UPDATE credentials SET expires_at=clock_timestamp()+interval '5 seconds' WHERE id=$1 RETURNING expires_at",&[&f.ctx.credential]).await.unwrap().get(0);
    let held = blocker.transaction().await.unwrap();
    materialize(&held).await.unwrap();
    let before: i64 = admin
        .query_one(
            "SELECT count(*) FROM outbox WHERE topic='kernel.event.publish'",
            &[],
        )
        .await
        .unwrap()
        .get(0);
    let app = f.base.app.clone();
    let ctx = f.ctx.clone();
    let body = f.body("expired-while-storage-blocked", 2);
    let (sent_pid, received_pid) = tokio::sync::oneshot::channel();
    let writer = tokio::spawn(async move {
        let mut c = app.db.connect().await.unwrap();
        let pid: i32 = c
            .query_one("SELECT pg_backend_pid()", &[])
            .await
            .unwrap()
            .get(0);
        sent_pid.send(pid).unwrap();
        let tx = c.transaction().await.unwrap();
        db::auth_lock(&tx, false).await.unwrap();
        let result = append(&tx, &app.auth, &ctx, &body).await;
        if result.is_ok() {
            tx.commit().await.unwrap();
        } else {
            tx.rollback().await.unwrap();
        }
        result
    });
    let writer_pid = received_pid.await.unwrap();
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        loop {
            let blocked: bool = admin
                .query_one(
                    "SELECT $1=ANY(pg_blocking_pids($2))",
                    &[&blocker_pid, &writer_pid],
                )
                .await
                .unwrap()
                .get(0);
            if blocked {
                break;
            }
            assert!(
                !writer.is_finished(),
                "publisher failed before reaching its storage fence"
            );
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("publisher did not reach the held storage fence before its initial credential expired");
    let remaining = (expiry - chrono::Utc::now()).num_milliseconds().max(0) as u64;
    tokio::time::sleep(std::time::Duration::from_millis(remaining + 100)).await;
    held.commit().await.unwrap();
    let result = tokio::time::timeout(std::time::Duration::from_secs(10), writer)
        .await
        .unwrap()
        .unwrap();
    assert!(
        result.is_err(),
        "an expired publisher cannot commit after a delayed storage fence"
    );
    let counts=admin.query_one("SELECT (SELECT count(*) FROM events WHERE dedupe_key='expired-while-storage-blocked'),(SELECT count(*) FROM event_dedupe WHERE dedupe_key='expired-while-storage-blocked'),(SELECT count(*) FROM outbox WHERE topic='kernel.event.publish')",&[]).await.unwrap();
    assert_eq!(counts.get::<_, i64>(0), 0);
    assert_eq!(counts.get::<_, i64>(1), 0);
    assert_eq!(counts.get::<_, i64>(2), before);
}
