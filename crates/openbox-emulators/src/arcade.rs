//! Arcade Room kiosk PIN hashing/verification (PBKDF2-HMAC-SHA256) compatible
//! byte-for-byte with `handlers/arcade.py`.

use base64::Engine;
use hmac::{Hmac, Mac};
use openbox_core::ApiError;
use rand::RngCore;
use serde_json::{Value, json};
use sha2::Sha256;
use subtle::ConstantTimeEq;

pub const PIN_ITERATIONS: u32 = 120_000;
pub const PIN_SALT_BYTES: usize = 16;
pub const PIN_MIN_LENGTH: usize = 4;
pub const PIN_MAX_LENGTH: usize = 12;

fn pbkdf2_hmac_sha256(password: &[u8], salt: &[u8], iterations: u32) -> [u8; 32] {
    let mut u = [0u8; 32];
    let mut t = [0u8; 32];
    let mut mac = Hmac::<Sha256>::new_from_slice(password).expect("HMAC key");
    mac.update(salt);
    mac.update(&1u32.to_be_bytes());
    let first = mac.finalize().into_bytes();
    u.copy_from_slice(&first);
    t.copy_from_slice(&first);
    for _ in 1..iterations {
        let mut m = Hmac::<Sha256>::new_from_slice(password).expect("HMAC key");
        m.update(&u);
        let next = m.finalize().into_bytes();
        u.copy_from_slice(&next);
        for i in 0..32 {
            t[i] ^= u[i];
        }
    }
    t
}

pub fn validate_pin(value: &str) -> Result<&str, ApiError> {
    if value.len() < PIN_MIN_LENGTH
        || value.len() > PIN_MAX_LENGTH
        || !value.bytes().all(|b| b.is_ascii_digit())
    {
        return Err(ApiError::new(
            400,
            "ARCADE_INVALID_PIN",
            format!("Museum PIN must be {PIN_MIN_LENGTH}-{PIN_MAX_LENGTH} digits."),
        ));
    }
    Ok(value)
}

pub fn hash_kiosk_pin(
    pin: &str,
    salt_override: Option<&[u8]>,
    iterations: u32,
) -> Result<String, ApiError> {
    validate_pin(pin)?;
    let mut random_salt = [0u8; PIN_SALT_BYTES];
    let salt: &[u8] = match salt_override {
        Some(s) => s,
        None => {
            rand::thread_rng().fill_bytes(&mut random_salt);
            &random_salt
        }
    };
    let digest = pbkdf2_hmac_sha256(pin.as_bytes(), salt, iterations);
    let b64_salt = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(salt);
    let b64_digest = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(digest);
    Ok(format!(
        "pbkdf2-sha256${iterations}${b64_salt}${b64_digest}"
    ))
}

pub fn verify_kiosk_pin(stored: &str, pin: &str) -> bool {
    if pin.len() < PIN_MIN_LENGTH
        || pin.len() > PIN_MAX_LENGTH
        || !pin.bytes().all(|b| b.is_ascii_digit())
    {
        return false;
    }
    let parts: Vec<&str> = stored.splitn(4, '$').collect();
    if parts.len() != 4 || parts[0] != "pbkdf2-sha256" {
        return false;
    }
    let Ok(iterations) = parts[1].parse::<u32>() else {
        return false;
    };
    if !(50_000..=500_000).contains(&iterations) {
        return false;
    }
    let Ok(salt) = base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(parts[2]) else {
        return false;
    };
    let Ok(expected) = base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(parts[3]) else {
        return false;
    };
    if expected.len() != 32 {
        return false;
    }
    let actual = pbkdf2_hmac_sha256(pin.as_bytes(), &salt, iterations);
    bool::from(actual.as_slice().ct_eq(expected.as_slice()))
}

pub fn kiosk_status(state: &Value) -> Value {
    let settings = state.get("settings").and_then(Value::as_object);
    let enabled = settings
        .and_then(|s| s.get("museum_kiosk_enabled"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let pin_set = settings
        .and_then(|s| s.get("museum_kiosk_pin_hash"))
        .and_then(Value::as_str)
        .is_some_and(|h| !h.is_empty());
    json!({
        "enabled": enabled,
        "pin_set": pin_set,
    })
}
