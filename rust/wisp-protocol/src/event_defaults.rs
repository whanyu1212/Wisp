//! Fill schema defaults into inbound live events before they are validated.
//!
//! The event schema describes exactly what the current backend emits, so every
//! property is listed as required. Python clients decode events through Pydantic,
//! which fills a missing field from its declared default. Filling the same
//! defaults here makes the Rust frontend accept exactly the events Python accepts,
//! so a backend that omits a defaulted field (for example one built before the
//! field was added) is decoded the same way by both. Fields without a schema
//! default, including computed ones such as `timestamp`, stay required.

use serde_json::Value;

/// Insert each missing defaulted property of ``value``, recursing into nested models.
///
/// ``schema`` is the event schema root, which holds the discriminator mapping and
/// the ``$defs`` every reference resolves against. Values that do not match the
/// expected shape are left untouched for schema validation to reject.
pub(crate) fn fill_event_defaults(schema: &Value, value: &mut Value) {
    let Some(definition) = value
        .get("type")
        .and_then(Value::as_str)
        .and_then(|discriminator| schema["discriminator"]["mapping"][discriminator].as_str())
        .and_then(|reference| resolve(schema, reference))
    else {
        return;
    };
    fill_object(schema, definition, value);
}

fn resolve<'a>(schema: &'a Value, reference: &str) -> Option<&'a Value> {
    schema["$defs"].get(reference.strip_prefix("#/$defs/")?)
}

fn fill_object(schema: &Value, definition: &Value, value: &mut Value) {
    let (Some(properties), Some(object)) =
        (definition["properties"].as_object(), value.as_object_mut())
    else {
        return;
    };
    for (name, property) in properties {
        if !object.contains_key(name) {
            if let Some(default) = property.get("default") {
                object.insert(name.clone(), default.clone());
            }
        }
        if let Some(member) = object.get_mut(name) {
            fill_property(schema, property, member);
        }
    }
}

/// Recurse into the models a property may hold: a reference, a nullable
/// reference (``anyOf``), or a list of either.
fn fill_property(schema: &Value, property: &Value, value: &mut Value) {
    if let Some(reference) = property["$ref"].as_str() {
        if let Some(definition) = resolve(schema, reference) {
            fill_object(schema, definition, value);
        }
    } else if let Some(members) = property["anyOf"].as_array() {
        if let Some(member) = members.iter().find(|member| matches_shape(member, value)) {
            fill_property(schema, member, value);
        }
    } else if property["type"] == "array" {
        if let Some(items) = value.as_array_mut() {
            for item in items {
                fill_property(schema, &property["items"], item);
            }
        }
    }
}

/// Pick the ``anyOf`` branch a value belongs to: objects take the reference
/// branch, and scalars (including ``null``) need no filling.
fn matches_shape(member: &Value, value: &Value) -> bool {
    match value {
        Value::Object(_) => member.get("$ref").is_some(),
        Value::Array(_) => member["type"] == "array",
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn schema() -> Value {
        json!({
            "discriminator": {"mapping": {"probe": "#/$defs/Probe"}},
            "$defs": {
                "Probe": {"properties": {
                    "type": {"const": "probe", "default": "probe"},
                    "id": {"type": "string"},
                    "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null},
                    "items": {"type": "array", "items": {"$ref": "#/$defs/Item"}, "default": []},
                    "child": {"anyOf": [{"$ref": "#/$defs/Item"}, {"type": "null"}], "default": null}
                }},
                "Item": {"properties": {
                    "name": {"type": "string"},
                    "flag": {"type": "boolean", "default": false}
                }}
            }
        })
    }

    #[test]
    fn fills_missing_defaults_at_every_depth() {
        let mut value = json!({
            "type": "probe", "id": "a",
            "items": [{"name": "x"}], "child": {"name": "y"}
        });
        fill_event_defaults(&schema(), &mut value);
        assert_eq!(
            value,
            json!({
                "type": "probe", "id": "a", "note": null,
                "items": [{"name": "x", "flag": false}],
                "child": {"name": "y", "flag": false}
            })
        );
    }

    #[test]
    fn leaves_required_present_and_unknown_values_untouched() {
        let schema = schema();
        // A missing required field is not invented; validation rejects it later.
        let mut missing = json!({"type": "probe"});
        fill_event_defaults(&schema, &mut missing);
        assert!(missing.get("id").is_none());
        // Present values, including explicit nulls, are never replaced.
        let mut present = json!({"type": "probe", "id": "a", "note": "kept", "child": null});
        fill_event_defaults(&schema, &mut present);
        assert_eq!(present["note"], "kept");
        assert_eq!(present["child"], Value::Null);
        // Unknown discriminators and malformed shapes are left for validation.
        let mut unknown = json!({"type": "future", "x": 1});
        fill_event_defaults(&schema, &mut unknown);
        assert_eq!(unknown, json!({"type": "future", "x": 1}));
        let mut malformed = json!({"type": "probe", "id": "a", "items": "nope"});
        fill_event_defaults(&schema, &mut malformed);
        assert_eq!(malformed["items"], "nope");
    }
}
