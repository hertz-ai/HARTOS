//! hart-bridge — PyO3 bridge exposing claw-code's Rust tools to HARTOS Python.
//!
//! Compiled as a cdylib (.pyd on Windows, .so on Linux) and imported directly
//! by HARTOS's coding agent backend. Zero subprocess overhead.
//!
//! All functions return JSON strings — Python side parses them.
//! This keeps the FFI boundary clean and avoids complex type mapping.

use pyo3::prelude::*;
use pyo3::exceptions::PyRuntimeError;

// ─── Bash execution ─────────────────────────────────────────────────────────

#[pyfunction]
#[pyo3(signature = (command, timeout_ms=120000))]
fn execute_bash(command: &str, timeout_ms: u64) -> PyResult<String> {
    let input = runtime::BashCommandInput {
        command: command.to_string(),
        timeout: Some(timeout_ms),
        description: None,
        run_in_background: None,
        dangerously_disable_sandbox: None,
        namespace_restrictions: None,
        isolate_network: None,
        filesystem_mode: None,
        allowed_mounts: None,
    };
    match runtime::execute_bash(input) {
        Ok(output) => serde_json::to_string(&output)
            .map_err(|e| PyRuntimeError::new_err(format!("serialize: {e}"))),
        Err(e) => Err(PyRuntimeError::new_err(format!("bash: {e}"))),
    }
}

// ─── File operations ────────────────────────────────────────────────────────

#[pyfunction]
#[pyo3(signature = (path, offset=0, limit=2000))]
fn read_file(path: &str, offset: usize, limit: usize) -> PyResult<String> {
    match runtime::read_file(path, Some(offset), Some(limit)) {
        Ok(output) => serde_json::to_string(&output)
            .map_err(|e| PyRuntimeError::new_err(format!("serialize: {e}"))),
        Err(e) => Err(PyRuntimeError::new_err(format!("read: {e}"))),
    }
}

#[pyfunction]
fn write_file(path: &str, content: &str) -> PyResult<String> {
    match runtime::write_file(path, content) {
        Ok(output) => serde_json::to_string(&output)
            .map_err(|e| PyRuntimeError::new_err(format!("serialize: {e}"))),
        Err(e) => Err(PyRuntimeError::new_err(format!("write: {e}"))),
    }
}

#[pyfunction]
#[pyo3(signature = (path, old_string, new_string, replace_all=false))]
fn edit_file(path: &str, old_string: &str, new_string: &str, replace_all: bool) -> PyResult<String> {
    match runtime::edit_file(path, old_string, new_string, replace_all) {
        Ok(output) => serde_json::to_string(&output)
            .map_err(|e| PyRuntimeError::new_err(format!("serialize: {e}"))),
        Err(e) => Err(PyRuntimeError::new_err(format!("edit: {e}"))),
    }
}

// ─── Search operations ──────────────────────────────────────────────────────

#[pyfunction]
#[pyo3(signature = (pattern, path=None))]
fn glob_search(pattern: &str, path: Option<&str>) -> PyResult<String> {
    match runtime::glob_search(pattern, path) {
        Ok(output) => serde_json::to_string(&output)
            .map_err(|e| PyRuntimeError::new_err(format!("serialize: {e}"))),
        Err(e) => Err(PyRuntimeError::new_err(format!("glob: {e}"))),
    }
}

#[pyfunction]
#[pyo3(signature = (pattern, path=None, glob_pattern=None, case_insensitive=false))]
fn grep_search(
    pattern: &str,
    path: Option<&str>,
    glob_pattern: Option<&str>,
    case_insensitive: bool,
) -> PyResult<String> {
    let input = runtime::GrepSearchInput {
        pattern: pattern.to_string(),
        path: path.map(String::from),
        glob: glob_pattern.map(String::from),
        case_insensitive: Some(case_insensitive),
        output_mode: None,
        before: None,
        after: None,
        context_short: None,
        context: None,
        line_numbers: None,
        file_type: None,
        head_limit: None,
        offset: None,
        multiline: None,
    };
    match runtime::grep_search(&input) {
        Ok(output) => serde_json::to_string(&output)
            .map_err(|e| PyRuntimeError::new_err(format!("serialize: {e}"))),
        Err(e) => Err(PyRuntimeError::new_err(format!("grep: {e}"))),
    }
}

// ─── Python module ──────────────────────────────────────────────────────────

#[pymodule]
fn claw_bridge(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(execute_bash, m)?)?;
    m.add_function(wrap_pyfunction!(read_file, m)?)?;
    m.add_function(wrap_pyfunction!(write_file, m)?)?;
    m.add_function(wrap_pyfunction!(edit_file, m)?)?;
    m.add_function(wrap_pyfunction!(glob_search, m)?)?;
    m.add_function(wrap_pyfunction!(grep_search, m)?)?;
    Ok(())
}
