//! Loads the swap tables of `maps/translation` and the rules of `maps/patterns.json`.

use std::collections::BTreeMap;
use std::path::Path;

use serde_json::Value;

pub type Table = BTreeMap<String, String>;

/// One rule of `patterns.json`: when it applies and how it uses the maps.
pub struct Pattern {
    pub name: String,
    pub when: BTreeMap<String, Value>,
}

pub struct Maps {
    pub declarations: Table,
    pub types: Table,
    pub generic_types: Table,
    pub union_none: String,
    pub returns: Table,
    pub binary: Table,
    pub compare: Table,
    pub unary: Table,
    pub augmented: Table,
    pub operators: Table,
    pub literals: Table,
    pub statements: Table,
    /// Canonical Python name → Rust expression building its value.
    pub names: BTreeMap<String, String>,
    pub methods: Table,
    pub properties: Table,
    pub exceptions: Vec<(String, Vec<String>)>,
    pub fixtures: Table,
    pub marks: Table,
    pub patterns: Vec<Pattern>,
    /// Dotted prefixes of the code under migration.
    pub capabilities: Vec<String>,
}

fn read(path: &Path) -> Value {
    let text = std::fs::read_to_string(path)
        .unwrap_or_else(|e| panic!("cannot read {}: {e}", path.display()));
    serde_json::from_str(&text).unwrap_or_else(|e| panic!("invalid {}: {e}", path.display()))
}

/// String entries of an object, skipping `_about` notes.
fn table(value: &Value) -> Table {
    value
        .as_object()
        .into_iter()
        .flatten()
        .filter(|(k, _)| k.as_str() != "_about")
        .filter_map(|(k, v)| v.as_str().map(|s| (k.clone(), s.to_owned())))
        .collect()
}

impl Maps {
    pub fn load(root: &Path) -> Self {
        let t = |name: &str| read(&root.join("translation").join(name));
        let types = t("type-hints.json");
        let operators = t("operators.json");
        let stdlib = t("stdlib.json");
        let methods = t("methods.json");
        let doubles = t("doubles.json");
        let mut names = BTreeMap::new();
        let function = |key: &str, path: &str| format!("object::func({key:?}, {path})");
        for (key, path) in table(&t("builtins.json")) {
            names.insert(key.clone(), function(&key, &path));
        }
        for (key, path) in table(&stdlib["functions"]) {
            names.insert(key.clone(), function(&key, &path));
        }
        for (key, value) in table(&stdlib["values"]) {
            names.insert(key, value);
        }
        for (key, path) in table(&doubles["names"]) {
            names.insert(key.clone(), function(&key, &path));
        }
        let exceptions: Vec<(String, Vec<String>)> = t("exceptions.json")["classes"]
            .as_array()
            .expect("exception classes")
            .iter()
            .map(|entry| {
                let name = entry[0].as_str().expect("name").to_owned();
                let bases = entry[1]
                    .as_array()
                    .expect("bases")
                    .iter()
                    .map(|b| b.as_str().expect("base").to_owned())
                    .collect();
                (name, bases)
            })
            .collect();
        let patterns = read(&root.join("patterns.json"))["patterns"]
            .as_array()
            .expect("patterns")
            .iter()
            .map(|p| Pattern {
                name: p["pattern"].as_str().expect("pattern name").to_owned(),
                when: p["when"]
                    .as_object()
                    .map(|w| w.iter().map(|(k, v)| (k.clone(), v.clone())).collect())
                    .unwrap_or_default(),
            })
            .collect();
        Self {
            declarations: table(&t("declarations.json")),
            types: table(&types["simple"]),
            generic_types: table(&types["generic"]),
            union_none: types["union with None"].as_str().expect("union").to_owned(),
            returns: table(&t("returns.json")),
            binary: table(&operators["binary"]),
            compare: table(&operators["compare"]),
            unary: table(&operators["unary"]),
            augmented: table(&operators["augmented"]),
            operators: table(&operators),
            literals: table(&t("literals.json")),
            statements: table(&t("statements.json")),
            names,
            methods: table(&methods["methods"]),
            properties: table(&methods["properties"]),
            exceptions,
            fixtures: table(&doubles["fixtures"]),
            marks: table(&t("marks.json")),
            patterns,
            capabilities: stdlib["capability prefixes"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(str::to_owned))
                        .collect()
                })
                .unwrap_or_default(),
        }
    }

    pub fn is_exception(&self, canonical: &str) -> bool {
        self.exceptions.iter().any(|(n, _)| n == canonical)
    }

    pub fn is_known(&self, canonical: &str) -> bool {
        self.names.contains_key(canonical) || self.is_exception(canonical)
    }
}

/// Fill `{key}`, `{key:?}` (Rust string literal) and `{{`/`}}` in a template.
pub fn fill(template: &str, values: &[(&str, &str)]) -> String {
    let mut out = String::with_capacity(template.len());
    let mut chars = template.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '{' if chars.peek() == Some(&'{') => {
                chars.next();
                out.push('{');
            }
            '}' if chars.peek() == Some(&'}') => {
                chars.next();
                out.push('}');
            }
            '{' => {
                let field: String = chars.by_ref().take_while(|c| *c != '}').collect();
                let (key, debug) = match field.strip_suffix(":?") {
                    Some(key) => (key, true),
                    None => (field.as_str(), false),
                };
                let value = values
                    .iter()
                    .find(|(k, _)| *k == key)
                    .map(|(_, v)| *v)
                    .unwrap_or_else(|| panic!("template field {key} missing in {template}"));
                if debug {
                    out.push_str(&format!("{value:?}"));
                } else {
                    out.push_str(value);
                }
            }
            other => out.push(other),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fill_replaces_fields_and_escapes_braces() {
        assert_eq!(
            fill("fn {n}() {{ {b} }}", &[("n", "f"), ("b", "1")]),
            "fn f() { 1 }"
        );
        assert_eq!(fill("x({s:?})", &[("s", "a\"b")]), "x(\"a\\\"b\")");
    }

    #[test]
    fn every_map_loads() {
        let maps = Maps::load(&Path::new(env!("CARGO_MANIFEST_DIR")).join("maps"));
        assert_eq!(maps.patterns.len(), 10);
        assert_eq!(maps.types["int"], "i64");
        assert!(maps.names["builtins.len"].contains("natives::len"));
        assert!(maps.is_exception("builtins.ValueError"));
    }
}
