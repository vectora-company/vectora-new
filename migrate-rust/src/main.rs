//! Generate the Rust backend of Vectora in `rust-backend/`: one module per backend
//! file and one test per pytest file, translated with the swap tables of
//! `maps/translation` and the rules of `maps/patterns.json`.

mod layout;
mod maps;
mod translate;

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use tree_sitter::Parser;

use layout::write;
use maps::Maps;

struct Options {
    tests: PathBuf,
    backend: PathBuf,
    maps: PathBuf,
    runtime: PathBuf,
    mocks: PathBuf,
    template: PathBuf,
    output: PathBuf,
    reports: PathBuf,
}

fn options() -> Options {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mut options = Options {
        tests: root.join("../tests"),
        backend: root.join("../backend"),
        maps: root.join("maps"),
        runtime: root.join("runtime"),
        mocks: root.join("mocks"),
        template: root.join("template"),
        output: root.join("../rust-backend"),
        reports: root.join("reports"),
    };
    let mut args = std::env::args().skip(1);
    while let Some(flag) = args.next() {
        let value = PathBuf::from(
            args.next()
                .unwrap_or_else(|| panic!("{flag} needs a value")),
        );
        match flag.as_str() {
            "--tests" => options.tests = value,
            "--backend" => options.backend = value,
            "--maps" => options.maps = value,
            "--output" => options.output = value,
            "--reports" => options.reports = value,
            other => panic!(
                "unknown option {other}; use --tests, --backend, --maps, --output, --reports"
            ),
        }
    }
    options
}

fn python_files(root: &Path) -> Vec<PathBuf> {
    let mut files: Vec<PathBuf> = walkdir::WalkDir::new(root)
        .into_iter()
        .filter_map(Result::ok)
        .map(|e| e.into_path())
        .filter(|p| p.extension().is_some_and(|e| e == "py"))
        .filter(|p| {
            p.file_name()
                .is_some_and(|n| n.to_string_lossy().starts_with("test_"))
        })
        .collect();
    files.sort();
    files
}

/// The shared table of names, methods and properties from the maps.
fn names_table(maps: &Maps) -> String {
    let mut names = String::new();
    for (name, bases) in &maps.exceptions {
        let bases = bases
            .iter()
            .map(|b| format!("{b:?}"))
            .collect::<Vec<_>>()
            .join(", ");
        names.push_str(&format!(
            "        ({name:?}, || Object::Class(object::exception_class({name:?}, &[{bases}]))),\n"
        ));
    }
    for (name, value) in &maps.names {
        names.push_str(&format!("        ({name:?}, || {value}),\n"));
    }
    let entries = |table: &maps::Table| {
        table
            .iter()
            .map(|(k, v)| format!("        ({k:?}, {v}),\n"))
            .collect::<String>()
    };
    format!(
        "//! Generated from maps/translation by vectora-test-migrator.\n#![allow(warnings)]\n\npub use vectora_runtime;\npub use vectora_test_mocks;\nuse vectora_runtime::object::{{self, Object, Table}};\nuse vectora_runtime::{{async_sqlite, graph, libraries, natives, stdlib, system}};\nuse vectora_test_mocks::doubles;\n\npub static TABLE: Table = Table {{\n    names: &[\n{names}    ],\n    methods: &[\n{}    ],\n    properties: &[\n{}    ],\n    capabilities: &[{}],\n}};\n",
        entries(&maps.methods),
        entries(&maps.properties),
        maps.capabilities
            .iter()
            .map(|c| format!("{c:?}"))
            .collect::<Vec<_>>()
            .join(", ")
    )
}

fn main() {
    let options = options();
    let maps = Maps::load(&options.maps);
    let output = &layout::normalize(&std::path::absolute(&options.output).expect("output path"));
    let inputs = [
        &options.tests,
        &options.backend,
        &options.maps,
        &options.runtime,
        &options.mocks,
        &options.template,
        &options.reports,
    ];
    layout::clear_output(output, &inputs);
    let mut parser = Parser::new();
    parser
        .set_language(&tree_sitter_python::LANGUAGE.into())
        .expect("python grammar");
    let original = std::fs::canonicalize(&options.tests).unwrap_or(options.tests.clone());
    let keep_crate = |rel: &Path| !rel.starts_with("target") && rel != Path::new("Cargo.lock");
    layout::copy_tree(&options.template, output, |_| true);
    layout::copy_tree(&options.runtime, &output.join("crates/runtime"), keep_crate);
    layout::copy_tree(&options.mocks, &output.join("crates/mocks"), keep_crate);
    let modules = layout::backend_modules(&options.backend, &output.join("src"));
    layout::copy_data(&options.backend, &output.join("assets"));
    layout::copy_data(&options.tests, &output.join("tests"));
    let mut test_targets: Vec<(String, String)> = Vec::new();
    let mut report = Vec::new();
    let mut pending_log = String::new();
    let mut shapes: BTreeMap<String, (usize, Vec<String>)> = BTreeMap::new();
    let (mut tests, mut cases, mut clean_tests) = (0usize, 0usize, 0usize);
    for (index, path) in python_files(&options.tests).iter().enumerate() {
        let rel = path
            .strip_prefix(&options.tests)
            .expect("relative")
            .to_string_lossy()
            .into_owned();
        let src = std::fs::read_to_string(path).expect("python source");
        let tree = parser.parse(&src, None).expect("parse");
        let source_file = original.join(&rel).display().to_string();
        let mut conftests = Vec::new();
        let mut directory = PathBuf::from(&rel);
        while directory.pop() {
            let candidate = original.join(&directory).join("conftest.py");
            if let Ok(text) = std::fs::read_to_string(&candidate) {
                let rel = directory.join("conftest.py").display().to_string();
                conftests.push((rel, text, candidate.display().to_string()));
            }
            if directory.as_os_str().is_empty() {
                break;
            }
        }
        let module =
            translate::Module::new(&maps, &src, &rel, &source_file).with_conftests(conftests);
        let output_module = module.translate(tree.root_node());
        let target = PathBuf::from("tests").join(&rel).with_extension("rs");
        write(&output.join(&target), &output_module.rust);
        test_targets.push((
            format!(
                "m{index:03}_{}",
                target.file_stem().expect("stem").to_string_lossy()
            ),
            target.display().to_string(),
        ));
        for function in &output_module.functions {
            if function.test_cases > 0 {
                tests += 1;
                cases += function.test_cases;
                clean_tests += usize::from(function.pending == 0);
            }
        }
        for pending in &output_module.pending {
            pending_log.push_str(&format!(
                "{rel}:{}: {}: [{}] {}\n",
                pending.line, pending.function, pending.kind, pending.source
            ));
            let entry = shapes.entry(pending.kind.clone()).or_default();
            entry.0 += 1;
            if entry.1.len() < 3 {
                entry.1.push(format!("{rel}:{}", pending.line));
            }
        }
        report.push(serde_json::json!({
            "file": rel,
            "functions": output_module.functions,
            "pending": output_module.pending.len(),
        }));
    }
    write(
        &output.join("Cargo.toml"),
        &layout::root_manifest(&test_targets),
    );
    write(
        &output.join("crates/test-support/Cargo.toml"),
        &layout::support_manifest(),
    );
    write(
        &output.join("crates/test-support/src/lib.rs"),
        &names_table(&maps),
    );
    write(
        &options.reports.join("unresolved-patterns.log"),
        &pending_log,
    );
    // The application is committed formatted, so `cargo fmt --check` holds.
    layout::format(output);
    let mut ranked: Vec<_> = shapes.into_iter().collect();
    ranked.sort_by(|a, b| b.1.0.cmp(&a.1.0).then(a.0.cmp(&b.0)));
    let summary = serde_json::json!({
        "backend_modules": modules,
        "files": report.len(),
        "tests": tests,
        "test_cases": cases,
        "tests_without_pending": clean_tests,
        "pending_by_kind": ranked.iter().map(|(k, (n, ex))| serde_json::json!({"kind": k, "count": n, "examples": ex})).collect::<Vec<_>>(),
        "files_detail": report,
    });
    write(
        &options.reports.join("report.json"),
        &serde_json::to_string_pretty(&summary).expect("report"),
    );
    println!(
        "{modules} módulos do backend; {} arquivos de teste, {tests} testes ({cases} casos), {clean_tests} sem pendência. Saída: {}",
        summary["files"],
        output.display()
    );
    for (kind, (count, _)) in ranked.iter().take(12) {
        println!("{count:5}  {kind}");
    }
}
