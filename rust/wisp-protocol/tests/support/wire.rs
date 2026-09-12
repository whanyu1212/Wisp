//! Shared wire-decoding oracle for stable regressions and instrumented fuzz targets.

use serde::{Serialize, de::DeserializeOwned};
use wisp_protocol::{commands, events, handshake_request, handshake_response};

fn round_trip<T: DeserializeOwned + Serialize>(bytes: &[u8]) -> bool {
    // Parsing Value first would erase duplicate object keys before validation.
    let Ok(value) = serde_json::from_slice::<T>(bytes) else {
        return false;
    };
    let canonical = serde_json::to_vec(&value).expect("validated value must serialize");
    let decoded: T = serde_json::from_slice(&canonical).expect("canonical value must decode");
    assert_eq!(
        serde_json::to_value(value).unwrap(),
        serde_json::to_value(decoded).unwrap(),
        "canonical wire representation must be stable"
    );
    true
}

pub fn client_wire(bytes: &[u8]) -> bool {
    let command = round_trip::<commands::WispTypedClientRpcCommands>(bytes);
    let handshake = round_trip::<handshake_request::RpcHandshakeRequest>(bytes);
    command || handshake
}

pub fn server_wire(bytes: &[u8]) -> bool {
    let event = round_trip::<events::WispCurrentLiveEventOutput>(bytes);
    let handshake = round_trip::<handshake_response::RpcHandshakeResponse>(bytes);
    event || handshake
}
