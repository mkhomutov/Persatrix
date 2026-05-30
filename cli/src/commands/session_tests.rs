//! Unit tests for `persatrix session` output rendering.

use super::*;
use crate::types::SessionRow;

fn sample(id: &str, label: &str, archived: bool) -> SessionRow {
    SessionRow {
        id: id.to_string(),
        label: label.to_string(),
        created_at: "2026-05-30T12:00:00Z".to_string(),
        archived,
    }
}

#[test]
fn render_list_empty_non_json_is_friendly() {
    assert_eq!(render_list(&[], false), "No sessions.");
}

#[test]
fn render_list_empty_json_is_empty_array() {
    assert_eq!(render_list(&[], true), "[]");
}

#[test]
fn render_list_table_carries_headers_and_rows() {
    let rows = vec![sample("0190-abc", "alpha", false)];
    let out = render_list(&rows, false);
    assert!(out.contains("ID"));
    assert!(out.contains("LABEL"));
    assert!(out.contains("CREATED"));
    assert!(out.contains("ARCHIVED"));
    assert!(out.contains("0190-abc"));
    assert!(out.contains("alpha"));
}

#[test]
fn render_list_json_carries_fields() {
    let rows = vec![sample("0190-abc", "alpha", true)];
    let out = render_list(&rows, true);
    assert!(out.contains("\"id\": \"0190-abc\""));
    assert!(out.contains("\"label\": \"alpha\""));
    assert!(out.contains("\"archived\": true"));
}

#[test]
fn render_one_table_carries_the_session() {
    let out = render_one(&sample("0190-xyz", "", false), false);
    assert!(out.contains("0190-xyz"));
    assert!(out.contains("ID"));
}

#[test]
fn render_one_json_carries_fields() {
    let out = render_one(&sample("0190-xyz", "beta", false), true);
    assert!(out.contains("\"id\": \"0190-xyz\""));
    assert!(out.contains("\"label\": \"beta\""));
    assert!(out.contains("\"archived\": false"));
}
