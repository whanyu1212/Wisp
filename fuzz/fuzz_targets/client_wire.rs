#![no_main]
#![forbid(unsafe_code)]

#[path = "../../rust/wisp-protocol/tests/support/wire.rs"]
#[allow(dead_code)]
mod wire;

libfuzzer_sys::fuzz_target!(|bytes: &[u8]| {
    wire::client_wire(bytes);
});
