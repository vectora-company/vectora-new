//! The Cargo application written to `rust-backend/`: manifests, copied manual crates,
//! the template files, the data files and one Rust module per backend file.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Component, Path, PathBuf};

pub fn write(path: &Path, contents: &str) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).expect("output directory");
    }
    std::fs::write(path, contents)
        .unwrap_or_else(|e| panic!("cannot write {}: {e}", path.display()));
}

/// Resolve `.` and `..` lexically.
pub fn normalize(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for part in path.components() {
        match part {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other),
        }
    }
    out
}

/// Empty the generated application, so stale files never survive. The build
/// cache and the lockfile stay. Refuses a directory that overlaps an input or
/// that holds something other than a previously generated application.
pub fn clear_output(output: &Path, inputs: &[&PathBuf]) {
    for input in inputs {
        let input = normalize(&std::path::absolute(input).expect("input path"));
        assert!(
            !output.starts_with(&input) && !input.starts_with(output),
            "output {} overlaps the input {}",
            output.display(),
            input.display()
        );
    }
    let Ok(entries) = std::fs::read_dir(output) else {
        return;
    };
    let entries: Vec<_> = entries.map(|e| e.expect("output entry")).collect();
    let generated = std::fs::read_to_string(output.join("Cargo.toml"))
        .is_ok_and(|m| m.contains("name = \"vectora-backend\""));
    assert!(
        entries.is_empty() || generated,
        "{} is not empty and is not a generated backend",
        output.display()
    );
    for entry in entries {
        if entry.file_name() == "target" || entry.file_name() == "Cargo.lock" {
            continue;
        }
        let path = entry.path();
        let removed = if entry.file_type().expect("file type").is_dir() {
            std::fs::remove_dir_all(&path)
        } else {
            std::fs::remove_file(&path)
        };
        removed.unwrap_or_else(|e| panic!("cannot remove {}: {e}", path.display()));
    }
}

/// Run rustfmt over the generated workspace.
pub fn format(output: &Path) {
    let cargo = std::env::var_os("CARGO").unwrap_or_else(|| "cargo".into());
    let status = std::process::Command::new(cargo)
        .args(["fmt", "--all", "--manifest-path"])
        .arg(output.join("Cargo.toml"))
        .status()
        .expect("run cargo fmt");
    assert!(status.success(), "cargo fmt failed on {}", output.display());
}

fn walk(root: &Path) -> Vec<PathBuf> {
    let mut files: Vec<PathBuf> = walkdir::WalkDir::new(root)
        .into_iter()
        .filter_entry(|e| {
            let name = e.file_name().to_string_lossy();
            name != "__pycache__" && name != "target"
        })
        .filter_map(Result::ok)
        .filter(|e| e.file_type().is_file())
        .map(|e| e.into_path())
        .collect();
    files.sort();
    files
}

fn is_python(path: &Path) -> bool {
    path.extension().is_some_and(|e| e == "py")
}

/// Copy every file under `from` that `keep` accepts, keeping relative paths.
pub fn copy_tree(from: &Path, to: &Path, keep: impl Fn(&Path) -> bool) -> usize {
    let mut copied = 0;
    for file in walk(from) {
        let rel = file.strip_prefix(from).expect("relative");
        if !keep(rel) {
            continue;
        }
        let target = to.join(rel);
        if let Some(parent) = target.parent() {
            std::fs::create_dir_all(parent).expect("output directory");
        }
        std::fs::copy(&file, &target)
            .unwrap_or_else(|e| panic!("cannot copy {}: {e}", file.display()));
        copied += 1;
    }
    copied
}

/// Data files (not Python) of a source tree, copied as they are.
pub fn copy_data(from: &Path, to: &Path) -> usize {
    copy_tree(from, to, |rel| !is_python(rel))
}

/// One module per backend file: `pkg/__init__.py` is `pkg/mod.rs` and the
/// package root is `lib.rs`. Returns the number of modules written.
pub fn backend_modules(backend: &Path, src: &Path) -> usize {
    let files: Vec<PathBuf> = walk(backend)
        .into_iter()
        .filter(|f| is_python(f))
        .map(|f| f.strip_prefix(backend).expect("relative").to_path_buf())
        .collect();
    // Package directory (relative) -> child module names.
    let mut children: BTreeMap<PathBuf, BTreeSet<String>> = BTreeMap::new();
    let mut packages: BTreeSet<PathBuf> = BTreeSet::from([PathBuf::new()]);
    for rel in &files {
        let mut directory = rel.parent().unwrap_or(Path::new("")).to_path_buf();
        packages.insert(directory.clone());
        let stem = rel
            .file_stem()
            .expect("stem")
            .to_string_lossy()
            .into_owned();
        if stem != "__init__" {
            children.entry(directory.clone()).or_default().insert(stem);
        }
        // Every enclosing directory declares the one below it.
        while let Some(parent) = directory.parent() {
            let name = directory
                .file_name()
                .expect("name")
                .to_string_lossy()
                .into_owned();
            children
                .entry(parent.to_path_buf())
                .or_default()
                .insert(name);
            packages.insert(parent.to_path_buf());
            directory = parent.to_path_buf();
        }
    }
    let mut written = 0;
    for package in &packages {
        let init = package.join("__init__.py");
        let origin = if files.contains(&init) {
            format!("backend/{}", init.display())
        } else {
            format!("backend/{} (namespace package)", package.display())
        };
        let mut text = format!("//! Translated from {origin}.\n");
        if package.as_os_str().is_empty() {
            // Module names mirror the Python files, including `main` and
            // packages holding a module with their own name.
            text.push_str("\n#![allow(special_module_name, clippy::module_inception)]\n");
        }
        let declared = children.get(package).cloned().unwrap_or_default();
        if !declared.is_empty() {
            text.push('\n');
        }
        for name in &declared {
            text.push_str(&format!("pub mod {name};\n"));
        }
        let target = if package.as_os_str().is_empty() {
            src.join("lib.rs")
        } else {
            src.join(package).join("mod.rs")
        };
        write(&target, &text);
        written += 1;
    }
    for rel in &files {
        if rel.file_stem().is_some_and(|s| s == "__init__") {
            continue;
        }
        write(
            &src.join(rel.with_extension("rs")),
            &format!("//! Translated from backend/{}.\n", rel.display()),
        );
        written += 1;
    }
    written
}

/// `rust-backend/Cargo.toml`: the workspace and the backend package with its tests.
pub fn root_manifest(tests: &[(String, String)]) -> String {
    let mut manifest = String::from(
        r#"[workspace]
members = [".", "crates/runtime", "crates/mocks", "crates/test-support"]
resolver = "3"

[package]
name = "vectora-backend"
version = "0.1.0"
edition = "2024"
license = "AGPL-3.0-only"
description = "Vectora backend in Rust"
autobins = false
autotests = false

[lib]
path = "src/lib.rs"

[[bin]]
name = "vectora-backend"
path = "src/bin/vectora-backend.rs"

[dev-dependencies]
vectora-runtime = { path = "crates/runtime" }
vectora-test-mocks = { path = "crates/mocks" }
vectora-test-support = { path = "crates/test-support" }

# Hundreds of test binaries: no debug info keeps compile and link within memory.
[profile.dev]
debug = 0
incremental = false
"#,
    );
    for (name, path) in tests {
        manifest.push_str(&format!("\n[[test]]\nname = {name:?}\npath = {path:?}\n"));
    }
    manifest
}

/// `rust-backend/crates/test-support/Cargo.toml`: the crate holding the shared name table.
pub fn support_manifest() -> String {
    r#"[package]
name = "vectora-test-support"
version = "0.1.0"
edition = "2024"
license = "AGPL-3.0-only"
description = "Name table shared by the translated tests"

[dependencies]
vectora-runtime = { path = "../runtime" }
vectora-test-mocks = { path = "../mocks" }
"#
    .into()
}
