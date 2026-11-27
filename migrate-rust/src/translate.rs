//! Walks a Python module and emits Rust by applying the maps and patterns.
//!
//! The walker only chooses which map entry or pattern applies. The Rust text
//! itself comes from the templates in `maps/`.

use std::collections::{BTreeMap, BTreeSet};

use tree_sitter::Node;

use crate::maps::{Maps, fill};

/// A fragment that no pattern or map entry recognised.
#[derive(Clone, serde::Serialize)]
pub struct Pending {
    pub function: String,
    pub line: usize,
    pub kind: String,
    pub source: String,
}

#[derive(Clone, serde::Serialize)]
pub struct FunctionReport {
    pub name: String,
    pub line: usize,
    pub pattern: String,
    pub test_cases: usize,
    pub pending: usize,
}

pub struct ModuleOutput {
    pub rust: String,
    pub functions: Vec<FunctionReport>,
    pub pending: Vec<Pending>,
}

const PYTHON_BUILTINS: &[&str] = &[
    "abs",
    "all",
    "any",
    "bool",
    "bytes",
    "callable",
    "chr",
    "dict",
    "dir",
    "divmod",
    "enumerate",
    "filter",
    "float",
    "format",
    "frozenset",
    "getattr",
    "hasattr",
    "hash",
    "id",
    "int",
    "isinstance",
    "issubclass",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "object",
    "open",
    "ord",
    "print",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "setattr",
    "sorted",
    "str",
    "sum",
    "super",
    "tuple",
    "type",
    "vars",
    "zip",
    "__import__",
];

const RUST_UNRAW: &[&str] = &["self", "super", "crate", "Self", "_"];

pub fn ident(name: &str) -> String {
    if RUST_UNRAW.contains(&name) {
        format!("{name}__")
    } else {
        format!("r#{name}")
    }
}

#[derive(Clone)]
enum Item {
    /// Module function: Rust name of the `fn(Args) -> Object` to call.
    Function(String),
    /// Module constant.
    Constant(String),
}

#[derive(Clone)]
struct Fixture {
    function: String,
    parameters: Vec<String>,
    autouse: bool,
    method_of: Option<String>,
}

pub struct Module<'a> {
    maps: &'a Maps,
    src: &'a str,
    rel: String,
    source_file: String,
    module_path: String,
    aliases: BTreeMap<String, String>,
    items: BTreeMap<String, Item>,
    fixtures: BTreeMap<String, Fixture>,
    out: Vec<String>,
    local_names: Vec<String>,
    local_methods: Vec<String>,
    local_properties: Vec<String>,
    cases: Vec<String>,
    functions: Vec<FunctionReport>,
    pending: Vec<Pending>,
    counter: usize,
    /// `conftest.py` files that apply to this module, nearest first.
    conftests: Vec<(String, String, String)>,
    /// Fixtures translated from a `conftest.py`.
    borrowed: Vec<String>,
}

/// Per-function state: which names are locals and how control flow leaves.
#[derive(Clone)]
struct Scope {
    function: String,
    locals: BTreeSet<String>,
    aliases: BTreeMap<String, String>,
    /// Inside a `with`/`try` closure that returns `Flow`.
    in_block: bool,
    loop_in_block: bool,
    in_loop: bool,
    typed_return: Option<String>,
    generator: bool,
    in_handler: bool,
    /// Completion flag of the innermost loop that has an `else`.
    loop_flag: Option<String>,
    /// Locals shared with nested functions through `nonlocal`.
    cells: BTreeSet<String>,
    /// Class and receiver parameter of the method being translated.
    owner: Option<(String, String)>,
    /// Class whose methods are being translated inside a function.
    defining_class: Option<String>,
}

/// `(name, Rust type, default)` of each parameter, and the return type.
type Signature = (Vec<(String, String, String)>, String);

fn text<'s>(node: Node<'_>, src: &'s str) -> &'s str {
    node.utf8_text(src.as_bytes()).unwrap_or("")
}

fn named(node: Node<'_>) -> Vec<Node<'_>> {
    let mut cursor = node.walk();
    node.named_children(&mut cursor)
        .filter(|n| n.kind() != "comment")
        .collect()
}

fn children(node: Node<'_>) -> Vec<Node<'_>> {
    let mut cursor = node.walk();
    node.children(&mut cursor).collect()
}

fn field<'t>(node: Node<'t>, name: &str) -> Option<Node<'t>> {
    node.child_by_field_name(name)
}

fn indent(code: &str) -> String {
    code.lines()
        .map(|l| {
            if l.is_empty() {
                String::new()
            } else {
                format!("    {l}\n")
            }
        })
        .collect()
}

impl<'a> Module<'a> {
    pub fn new(maps: &'a Maps, src: &'a str, rel: &str, source_file: &str) -> Self {
        let module_path = rel.trim_end_matches(".py").replace('/', ".");
        Self {
            maps,
            src,
            rel: rel.to_owned(),
            source_file: source_file.to_owned(),
            module_path,
            aliases: BTreeMap::new(),
            items: BTreeMap::new(),
            fixtures: BTreeMap::new(),
            out: Vec::new(),
            local_names: Vec::new(),
            local_methods: Vec::new(),
            local_properties: Vec::new(),
            cases: Vec::new(),
            functions: Vec::new(),
            pending: Vec::new(),
            counter: 0,
            conftests: Vec::new(),
            borrowed: Vec::new(),
        }
    }

    /// Source file of a module of the repository under migration.
    fn module_file(&self, module: &str) -> Option<String> {
        if !self
            .maps
            .capabilities
            .iter()
            .any(|p| format!("{module}.").starts_with(p.as_str()))
        {
            return None;
        }
        let root = std::path::Path::new(&self.source_file)
            .ancestors()
            .find(|p| p.join("pyproject.toml").is_file())?
            .to_path_buf();
        let base = root.join(module.replace('.', "/"));
        let file = if base.is_dir() {
            base.join("__init__.py")
        } else {
            base.with_extension("py")
        };
        Some(file.display().to_string())
    }

    /// `__file__` of a Python source, relative to the repository root (the
    /// directory with `pyproject.toml`) so the generated code names no machine.
    fn file_literal(&self, path: &str) -> String {
        let path = std::path::Path::new(path);
        let relative = path
            .ancestors()
            .find(|p| p.join("pyproject.toml").is_file())
            .and_then(|root| path.strip_prefix(root).ok())
            .unwrap_or(path);
        fill(
            &self.maps.literals["__file__"],
            &[("source_file", &relative.display().to_string())],
        )
    }

    /// `(relative path, source, absolute path)` of each applicable conftest.
    pub fn with_conftests(mut self, conftests: Vec<(String, String, String)>) -> Self {
        self.conftests = conftests;
        self
    }

    /// Translate a fixture defined in a `conftest.py`, through the same
    /// fixture pattern, into a module of its own inside this file.
    fn conftest_fixture(&mut self, name: &str) -> Option<Fixture> {
        let mut parser = tree_sitter::Parser::new();
        parser
            .set_language(&tree_sitter_python::LANGUAGE.into())
            .ok()?;
        for (index, (rel, src, file)) in self.conftests.clone().iter().enumerate() {
            let tree = parser.parse(src, None)?;
            let mut sub = Module::new(self.maps, src, rel, file);
            let statements = named(tree.root_node());
            for statement in &statements {
                sub.collect(*statement, None);
            }
            let Some(fixture) = sub.fixtures.get(name).cloned() else {
                continue;
            };
            // Other conftest helpers are not translated: they resolve by name.
            sub.items.clear();
            let definition = statements.iter().copied().find(|s| {
                let (decorators, definition) = unwrap_decorated(*s);
                definition.kind() == "function_definition"
                    && decorators
                        .iter()
                        .any(|d| sub.decorator_name(*d) == "pytest.fixture")
                    && format!("r#{}", sub.src(field(definition, "name").expect("name")))
                        == fixture.function
            })?;
            let (decorators, definition) = unwrap_decorated(definition);
            sub.function(definition, &decorators, None);
            let module = format!("conftest_{index}_{}", name.trim_start_matches('_'));
            let code: String = sub
                .out
                .iter()
                .map(|item| {
                    item.strip_prefix("fn ")
                        .map_or_else(|| item.clone(), |rest| format!("pub fn {rest}"))
                })
                .collect::<Vec<_>>()
                .join("\n");
            self.out.push(format!(
                "mod {module} {{\n    use super::*;\n{}}}\n",
                indent(&code)
            ));
            self.pending.extend(sub.pending);
            let borrowed = Fixture {
                function: format!("{module}::{}", fixture.function),
                parameters: fixture.parameters,
                autouse: false,
                method_of: None,
            };
            self.fixtures.insert(name.to_owned(), borrowed.clone());
            self.borrowed.push(name.to_owned());
            return Some(borrowed);
        }
        None
    }

    fn src(&self, node: Node<'_>) -> &'a str {
        text(node, self.src)
    }

    fn loc(&self, node: Node<'_>) -> String {
        format!("{}:{}", self.rel, node.start_position().row + 1)
    }

    fn fresh(&mut self, base: &str) -> String {
        self.counter += 1;
        format!("__{base}{}", self.counter)
    }

    fn pending(&mut self, scope: &str, node: Node<'_>, kind: &str) -> String {
        let source: String = self
            .src(node)
            .lines()
            .next()
            .unwrap_or("")
            .chars()
            .take(160)
            .collect();
        self.pending.push(Pending {
            function: scope.to_owned(),
            line: node.start_position().row + 1,
            kind: kind.to_owned(),
            source: source.clone(),
        });
        fill(
            &self.maps.statements["pending"],
            &[("loc", &self.loc(node)), ("source", &source)],
        )
    }

    // ------------------------------------------------------------ module

    pub fn translate(mut self, root: Node<'_>) -> ModuleOutput {
        let statements = named(root);
        for statement in &statements {
            self.collect(*statement, None);
        }
        for statement in &statements {
            self.module_statement(*statement);
        }
        let mut rust = String::new();
        rust.push_str(&format!(
            "// Generated by vectora-test-migrator from {}\n#![allow(warnings)]\n#![allow(clippy::all)]\n\n",
            self.rel
        ));
        rust.push_str("use vectora_runtime::object::{self, Args, Flow, Object, Table};\nuse vectora_runtime::{classes, convert, natives};\nuse vectora_test_mocks::doubles;\n");
        if let Some(imports) = self.maps.declarations.get("imports") {
            rust.push_str(imports);
        }
        rust.push_str("use std::collections::BTreeMap;\nuse std::path::PathBuf;\n\n");
        rust.push_str(&format!(
            "static LOCAL: Table = Table {{\n    names: &[\n{}    ],\n    methods: &[\n{}    ],\n    properties: &[\n{}    ],\n    capabilities: &[],\n}};\n\n",
            self.local_names.iter().map(|n| format!("        {n},\n")).collect::<String>(),
            self.local_methods.iter().map(|n| format!("        {n},\n")).collect::<String>(),
            self.local_properties.iter().map(|n| format!("        {n},\n")).collect::<String>(),
        ));
        rust.push_str(&self.out.join("\n"));
        rust.push_str("\n#[cfg(test)]\nmod cases {\n    use super::*;\n\n");
        for case in &self.cases {
            rust.push_str(&indent(case));
            rust.push('\n');
        }
        rust.push_str("}\n");
        ModuleOutput {
            rust,
            functions: self.functions,
            pending: self.pending,
        }
    }

    fn import(&mut self, node: Node<'_>, aliases: &mut BTreeMap<String, String>) {
        match node.kind() {
            "import_statement" => {
                for name in named(node) {
                    match name.kind() {
                        "aliased_import" => {
                            let module = self.src(field(name, "name").expect("name")).to_owned();
                            let alias = self.src(field(name, "alias").expect("alias")).to_owned();
                            aliases.insert(alias, module);
                        }
                        _ => {
                            let module = self.src(name).to_owned();
                            let first = module.split('.').next().unwrap_or(&module).to_owned();
                            aliases.insert(first.clone(), first);
                        }
                    }
                }
            }
            "import_from_statement" => {
                let module = field(node, "module_name")
                    .map(|m| self.src(m).to_owned())
                    .unwrap_or_default();
                let module = if module.starts_with('.') {
                    format!(
                        "{}{}",
                        self.module_path.rsplit_once('.').map_or("", |p| p.0),
                        module.trim_start_matches('.')
                    )
                } else {
                    module
                };
                let mut cursor = node.walk();
                for name in node.children_by_field_name("name", &mut cursor) {
                    let (imported, alias) = match name.kind() {
                        "aliased_import" => (
                            self.src(field(name, "name").expect("name")).to_owned(),
                            self.src(field(name, "alias").expect("alias")).to_owned(),
                        ),
                        _ => (self.src(name).to_owned(), self.src(name).to_owned()),
                    };
                    aliases.insert(alias, format!("{module}.{imported}"));
                }
            }
            _ => {}
        }
    }

    /// First pass: names defined by the module, so references resolve.
    fn collect(&mut self, statement: Node<'_>, class: Option<&str>) {
        let (decorators, definition) = unwrap_decorated(statement);
        match definition.kind() {
            "import_statement" | "import_from_statement" if class.is_none() => {
                let mut aliases = std::mem::take(&mut self.aliases);
                self.import(definition, &mut aliases);
                self.aliases = aliases;
            }
            "function_definition" => {
                let name = self
                    .src(field(definition, "name").expect("name"))
                    .to_owned();
                let rust = match class {
                    Some(c) => format!("{c}__{name}"),
                    None => name.clone(),
                };
                if decorators
                    .iter()
                    .any(|d| self.decorator_name(*d) == "pytest.fixture")
                {
                    let fixture_name = decorators
                        .iter()
                        .filter_map(|d| self.decorator_keyword(*d, "name"))
                        .next()
                        .unwrap_or_else(|| name.clone());
                    let autouse = decorators
                        .iter()
                        .any(|d| self.decorator_keyword(*d, "autouse").as_deref() == Some("True"));
                    let parameters = self
                        .parameter_names(definition)
                        .into_iter()
                        .filter(|p| p != "self")
                        .collect();
                    self.fixtures.insert(
                        fixture_name,
                        Fixture {
                            function: ident(&rust),
                            parameters,
                            autouse,
                            method_of: class.map(str::to_owned),
                        },
                    );
                } else if class.is_none() {
                    // A typed helper is called through its adapter.
                    let rust =
                        if self.pattern_of(definition, &decorators, class) == "auxiliar tipado" {
                            format!("{rust}__call")
                        } else {
                            rust
                        };
                    self.items.insert(name, Item::Function(ident(&rust)));
                }
            }
            "class_definition" if class.is_none() => {
                let name = self
                    .src(field(definition, "name").expect("name"))
                    .to_owned();
                self.items
                    .insert(name.clone(), Item::Constant(format!("__class_{name}")));
                if let Some(body) = field(definition, "body") {
                    for item in named(body) {
                        self.collect(item, Some(&name));
                    }
                }
            }
            "expression_statement" if class.is_none() => {
                if let Some(assignment) = named(definition)
                    .first()
                    .filter(|a| a.kind() == "assignment")
                    && let Some(left) =
                        field(*assignment, "left").filter(|l| l.kind() == "identifier")
                {
                    let name = self.src(left).to_owned();
                    if name != "pytestmark" {
                        self.items
                            .insert(name.clone(), Item::Constant(format!("__const_{name}")));
                    }
                }
            }
            _ => {}
        }
    }

    fn module_scope(&self) -> Scope {
        Scope {
            function: "<module>".into(),
            locals: BTreeSet::new(),
            aliases: self.aliases.clone(),
            in_block: false,
            loop_in_block: false,
            in_loop: false,
            typed_return: None,
            generator: false,
            in_handler: false,
            loop_flag: None,
            cells: BTreeSet::new(),
            owner: None,
            defining_class: None,
        }
    }

    fn module_statement(&mut self, statement: Node<'_>) {
        let (decorators, definition) = unwrap_decorated(statement);
        match definition.kind() {
            "function_definition" => self.function(definition, &decorators, None),
            "class_definition" => self.class(definition, &decorators),
            "expression_statement" => {
                let Some(assignment) = named(definition).first().copied() else {
                    return;
                };
                if assignment.kind() != "assignment" {
                    if assignment.kind() != "string" {
                        self.pending("<module>", definition, "module statement");
                    }
                    return;
                }
                let Some(left) = field(assignment, "left").filter(|l| l.kind() == "identifier")
                else {
                    self.pending("<module>", definition, "module assignment target");
                    return;
                };
                let name = self.src(left).to_owned();
                if name == "pytestmark" {
                    return;
                }
                let mut scope = self.module_scope();
                let value = match field(assignment, "right") {
                    Some(right) => self.expression(right, &mut scope),
                    None => "Object::None".into(),
                };
                self.out.push(format!(
                    "fn __const_{name}() -> Object {{\n    static VALUE: std::sync::OnceLock<Object> = std::sync::OnceLock::new();\n    VALUE.get_or_init(|| {value}).clone()\n}}\n"
                ));
            }
            "import_statement"
            | "import_from_statement"
            | "future_import_statement"
            | "comment" => {}
            _ => {
                self.pending("<module>", definition, "module statement");
            }
        }
    }

    fn decorator_name(&self, decorator: Node<'_>) -> String {
        let expression = named(decorator)
            .into_iter()
            .next()
            .expect("decorator expression");
        let target = if expression.kind() == "call" {
            field(expression, "function").expect("callee")
        } else {
            expression
        };
        self.canonical(self.src(target), &self.aliases)
    }

    fn decorator_keyword(&self, decorator: Node<'_>, key: &str) -> Option<String> {
        let expression = named(decorator).into_iter().next()?;
        let arguments = field(expression, "arguments")?;
        named(arguments).into_iter().find_map(|argument| {
            (argument.kind() == "keyword_argument" && self.src(field(argument, "name")?) == key)
                .then(|| {
                    let value = self.src(field(argument, "value").expect("value"));
                    value.trim_matches(|c| c == '"' || c == '\'').to_owned()
                })
        })
    }

    /// `alias.rest` → canonical dotted name, when the root is an import.
    fn canonical(&self, dotted: &str, aliases: &BTreeMap<String, String>) -> String {
        let (root, rest) = dotted
            .split_once('.')
            .map_or((dotted, None), |(a, b)| (a, Some(b)));
        match (aliases.get(root), rest) {
            (Some(module), Some(rest)) => format!("{module}.{rest}"),
            (Some(module), None) => module.clone(),
            (None, _) => dotted.to_owned(),
        }
    }

    // --------------------------------------------------------- classes

    fn class(&mut self, node: Node<'_>, _decorators: &[Node<'_>]) {
        let name = self.src(field(node, "name").expect("name")).to_owned();
        let pending_before = self.pending.len();
        let mut scope = self.module_scope();
        scope.function = name.clone();
        let bases = self.class_bases(node, &mut scope);
        let mut attrs = Vec::new();
        let body = field(node, "body").expect("class body");
        for item in named(body) {
            let (decorators, definition) = unwrap_decorated(item);
            match definition.kind() {
                "function_definition" => {
                    let method = self
                        .src(field(definition, "name").expect("name"))
                        .to_owned();
                    self.function(definition, &decorators, Some(&name));
                    let value = format!(
                        "object::func({method:?}, {})",
                        ident(&format!("{name}__{method}"))
                    );
                    if let Some(value) = self.wrap_method(value, &decorators, &mut scope, item) {
                        attrs.push(format!("({method:?}, {value})"))
                    }
                }
                _ => {
                    if let Some(attr) = self.class_attribute(definition, &mut scope) {
                        attrs.push(attr);
                    }
                }
            }
        }
        let value = fill(
            &self.maps.declarations["class"],
            &[
                ("name", &name),
                ("bases", &bases.join(", ")),
                ("attrs", &attrs.join(", ")),
            ],
        );
        self.out.push(
            fill(
                &self.maps.declarations["module class"],
                &[("name", &format!("__class_{name}")), ("value", &value)],
            ) + "\n",
        );
        self.functions.push(FunctionReport {
            name: name.clone(),
            line: node.start_position().row + 1,
            pattern: "classe".into(),
            test_cases: 0,
            pending: self.pending.len() - pending_before,
        });
    }

    fn class_bases(&mut self, node: Node<'_>, scope: &mut Scope) -> Vec<String> {
        field(node, "superclasses")
            .map(named)
            .unwrap_or_default()
            .into_iter()
            .filter(|b| b.kind() != "keyword_argument")
            .map(|b| self.expression(b, scope))
            .collect()
    }

    /// `property`, `staticmethod` and `classmethod` by template; other decorators are called.
    fn wrap_method(
        &mut self,
        value: String,
        decorators: &[Node<'_>],
        scope: &mut Scope,
        _node: Node<'_>,
    ) -> Option<String> {
        let mut value = value;
        for decorator in decorators.iter().rev() {
            let name = self.decorator_name(*decorator);
            let key = format!("decorator {}", name.rsplit('.').next().unwrap_or(&name));
            match self.maps.declarations.get(&key) {
                Some(template) => value = fill(template, &[("0", &value)]),
                // Test marks and patches are applied by the test wrapper.
                None if name.starts_with("pytest.") || self.maps.marks.contains_key(&name) => {}
                None => {
                    let expression = named(*decorator).into_iter().next().expect("decorator");
                    let callee = self.expression(expression, scope);
                    value = fill(
                        &self.maps.declarations["decorator"],
                        &[("decorator", &callee), ("value", &value)],
                    );
                }
            }
        }
        Some(value)
    }

    fn class_attribute(&mut self, definition: Node<'_>, scope: &mut Scope) -> Option<String> {
        if definition.kind() != "expression_statement" {
            return None;
        }
        let assignment = named(definition)
            .first()
            .copied()
            .filter(|a| a.kind() == "assignment")?;
        let left = field(assignment, "left").filter(|l| l.kind() == "identifier")?;
        let attribute = self.src(left).to_owned();
        let right = field(assignment, "right")?;
        let value = self.expression(right, scope);
        Some(format!("({attribute:?}, {value})"))
    }

    /// A class defined inside a function: methods are closures.
    fn nested_class(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let name = self.src(field(node, "name").expect("name")).to_owned();
        let bases = self.class_bases(node, scope);
        let mut attrs = Vec::new();
        for item in named(field(node, "body").expect("class body")) {
            let (decorators, definition) = unwrap_decorated(item);
            if definition.kind() == "function_definition" {
                let method = self
                    .src(field(definition, "name").expect("name"))
                    .to_owned();
                scope.defining_class = Some(name.clone());
                let value = self.nested_value(definition, scope);
                scope.defining_class = None;
                if let Some(value) = self.wrap_method(value, &decorators, scope, item) {
                    attrs.push(format!("({method:?}, {value})"));
                }
            } else if let Some(attr) = self.class_attribute(definition, scope) {
                attrs.push(attr);
            }
        }
        let value = fill(
            &self.maps.declarations["class"],
            &[
                ("name", &name),
                ("bases", &bases.join(", ")),
                ("attrs", &attrs.join(", ")),
            ],
        );
        scope.locals.insert(name.clone());
        format!("{} = {value};", ident(&name))
    }

    // ------------------------------------------------------- functions

    fn parameter_names(&self, function: Node<'_>) -> Vec<String> {
        let Some(parameters) = field(function, "parameters") else {
            return Vec::new();
        };
        named(parameters)
            .into_iter()
            .filter_map(|p| {
                let name = match p.kind() {
                    "identifier" => p,
                    "typed_parameter" => named(p).into_iter().next()?,
                    "default_parameter" | "typed_default_parameter" => field(p, "name")?,
                    _ => return None,
                };
                Some(self.src(name).to_owned())
            })
            .collect()
    }

    fn select_pattern(&self, facts: &BTreeMap<&str, serde_json::Value>) -> String {
        for pattern in &self.maps.patterns {
            let applies = pattern
                .when
                .iter()
                .all(|(key, expected)| match key.as_str() {
                    "decorator" => facts
                        .get("decorators")
                        .and_then(|d| d.as_array())
                        .is_some_and(|d| d.iter().any(|x| x == expected)),
                    other => facts.get(other).is_some_and(|actual| actual == expected),
                });
            if applies {
                return pattern.name.clone();
            }
        }
        "auxiliar sem tipos".into()
    }

    /// The pattern a function definition falls into.
    fn pattern_of(&self, node: Node<'_>, decorators: &[Node<'_>], class: Option<&str>) -> String {
        let name = self.src(field(node, "name").expect("name"));
        let is_async = children(node)
            .first()
            .is_some_and(|c| self.src(*c) == "async");
        let body = field(node, "body").expect("function body");
        let decorator_names: Vec<String> =
            decorators.iter().map(|d| self.decorator_name(*d)).collect();
        let parameters = self.parameter_names(node);
        let is_test =
            name.starts_with("test_") && !decorator_names.iter().any(|d| d == "pytest.fixture");
        let typed = self.typed_signature(node);
        let facts: BTreeMap<&str, serde_json::Value> = [
            ("decorators", serde_json::json!(decorator_names)),
            ("test", serde_json::json!(is_test)),
            ("async", serde_json::json!(is_async)),
            ("yields", serde_json::json!(contains_kind(body, "yield"))),
            (
                "parameters",
                serde_json::json!(parameters.iter().any(|p| p != "self")),
            ),
            (
                "all types mapped",
                serde_json::json!(typed.is_some() && class.is_none()),
            ),
        ]
        .into_iter()
        .collect();
        self.select_pattern(&facts)
    }

    fn function(&mut self, node: Node<'_>, decorators: &[Node<'_>], class: Option<&str>) {
        let name = self.src(field(node, "name").expect("name")).to_owned();
        let qualified = class.map_or_else(|| name.clone(), |c| format!("{c}.{name}"));
        let rust_name = match class {
            Some(c) => ident(&format!("{c}__{name}")),
            None => ident(&name),
        };
        let is_async = children(node)
            .first()
            .is_some_and(|c| self.src(*c) == "async");
        let body = field(node, "body").expect("function body");
        let yields = contains_kind(body, "yield");
        let decorator_names: Vec<String> =
            decorators.iter().map(|d| self.decorator_name(*d)).collect();
        let is_test =
            name.starts_with("test_") && !decorator_names.iter().any(|d| d == "pytest.fixture");
        let typed = self.typed_signature(node);
        let pattern = self.pattern_of(node, decorators, class);
        let pending_before = self.pending.len();

        let mut scope = Scope {
            function: qualified.clone(),
            locals: BTreeSet::new(),
            aliases: self.aliases.clone(),
            in_block: false,
            loop_in_block: false,
            in_loop: false,
            typed_return: None,
            generator: false,
            in_handler: false,
            loop_flag: None,
            cells: BTreeSet::new(),
            owner: class.and_then(|c| {
                self.parameter_names(node)
                    .into_iter()
                    .next()
                    .map(|p| (c.to_owned(), p))
            }),
            defining_class: None,
        };
        let binds = self.bind_parameters(node, &mut scope);
        let typed = typed.filter(|_| pattern == "auxiliar tipado");
        if let Some((_, ret)) = &typed {
            scope.typed_return = Some(ret.clone());
        }
        let locals = collect_locals(body, self.src);
        let mut declare = String::new();
        for local in &locals {
            if scope.locals.insert(local.clone()) {
                declare.push_str(&fill(
                    &self.maps.declarations["local"],
                    &[("local", &ident(local))],
                ));
                declare.push('\n');
            }
        }
        declare.push_str(&self.declare_cells(body, &mut scope));
        let body_kind = if yields {
            "generator body"
        } else if is_async {
            "coroutine body"
        } else {
            "plain"
        };
        scope.generator = yields;
        let statements = self.block(body, &mut scope);
        let code = match body_kind {
            "plain" => {
                let end = match &scope.typed_return {
                    Some(ty) => fill(
                        &self.maps.returns["end typed"],
                        &[("type", ty), ("loc", &self.loc(node))],
                    ),
                    None => self.maps.returns["end dynamic"].clone(),
                };
                format!(
                    "{}{}{}",
                    indent(&declare),
                    indent(&statements),
                    indent(&end)
                )
            }
            kind => indent(&fill(
                &self.maps.declarations[kind],
                &[(
                    "body",
                    &format!("{}{}", indent(&declare), indent(&statements)),
                )],
            )),
        };
        match &typed {
            Some((params, ret)) => {
                let signature_params = params
                    .iter()
                    .map(|(n, t, _)| format!("{}__arg: {t}", n))
                    .collect::<Vec<_>>()
                    .join(", ");
                let typed_binds: String = params
                    .iter()
                    .map(|(n, _, _)| {
                        format!(
                            "    {}\n",
                            fill(
                                &self.maps.declarations["typed parameter"],
                                &[("local", &ident(n)), ("arg", &format!("{n}__arg"))]
                            )
                        )
                    })
                    .collect();
                self.out.push(
                    fill(
                        &self.maps.declarations["typed function"],
                        &[
                            ("name", &rust_name),
                            ("params", &signature_params),
                            ("ret", ret),
                            ("binds", &typed_binds),
                            ("body", &code),
                            ("end", ""),
                        ],
                    ) + "\n",
                );
                let converted = params
                    .iter()
                    .enumerate()
                    .map(|(i, (n, t, default))| {
                        format!(
                            "convert::to::<{t}>(&__args.bind({i}, {n:?}, {default}), {:?})",
                            self.loc(node)
                        )
                    })
                    .collect::<Vec<_>>()
                    .join(", ");
                let adapter = fill(
                    &self.maps.declarations["typed adapter"],
                    &[("name", &rust_name), ("converted", &converted)],
                );
                self.out.push(
                    adapter.replacen(
                        &format!("fn {rust_name}__call"),
                        &format!(
                            "fn {}",
                            ident(&format!("{}__call", rust_name.trim_start_matches("r#")))
                        ),
                        1,
                    ) + "\n",
                );
                self.items.insert(
                    name.clone(),
                    Item::Function(ident(&format!(
                        "{}__call",
                        rust_name.trim_start_matches("r#")
                    ))),
                );
            }
            None => {
                self.out.push(
                    fill(
                        &self.maps.declarations["function"],
                        &[
                            ("name", &rust_name),
                            ("binds", &indent(&binds)),
                            ("body", &code),
                            ("end", ""),
                        ],
                    ) + "\n",
                );
            }
        }
        let mut test_cases = 0;
        if is_test {
            test_cases = self.test_wrappers(node, decorators, &name, &qualified, &rust_name, class);
        }
        self.functions.push(FunctionReport {
            name: qualified,
            line: node.start_position().row + 1,
            pattern,
            test_cases,
            pending: self.pending.len() - pending_before,
        });
    }

    /// Parameters, types and defaults when every annotation is in the type map.
    fn typed_signature(&self, node: Node<'_>) -> Option<Signature> {
        let ret = field(node, "return_type")?;
        let ret = self.rust_type(ret)?;
        let parameters = field(node, "parameters")?;
        let mut out = Vec::new();
        let scope = self.module_scope();
        for p in named(parameters) {
            let (name, ty, default) = match p.kind() {
                "typed_parameter" => (named(p).into_iter().next()?, field(p, "type")?, None),
                "typed_default_parameter" => {
                    (field(p, "name")?, field(p, "type")?, field(p, "value"))
                }
                _ => return None,
            };
            if name.kind() != "identifier" {
                return None;
            }
            let ty = self.rust_type(ty)?;
            let default = match default {
                Some(value) => {
                    let mut s = scope.clone();
                    // Defaults are simple literals in practice; others keep the dynamic pattern.
                    if !matches!(
                        value.kind(),
                        "integer" | "float" | "string" | "true" | "false" | "none"
                    ) {
                        return None;
                    }
                    let _ = &mut s;
                    format!("Some({})", self.literal_value(value)?)
                }
                None => "None".into(),
            };
            out.push((self.src(name).to_owned(), ty, default));
        }
        Some((out, ret))
    }

    fn literal_value(&self, node: Node<'_>) -> Option<String> {
        let mut scope = self.module_scope();
        // Literals never need `&mut self` effects; translate through a scratch module.
        let mut scratch = Module::new(self.maps, self.src, &self.rel, &self.source_file);
        Some(scratch.expression(node, &mut scope))
    }

    /// Annotation → Rust type through the type-hints map.
    fn rust_type(&self, node: Node<'_>) -> Option<String> {
        let node = if node.kind() == "type" {
            named(node).into_iter().next()?
        } else {
            node
        };
        match node.kind() {
            "identifier" | "attribute" | "none" => self.maps.types.get(self.src(node)).cloned(),
            "string" => {
                let inner = self.src(node).trim_matches(|c| c == '"' || c == '\'');
                self.maps.types.get(inner).cloned()
            }
            "binary_operator" => {
                let left = field(node, "left")?;
                let right = field(node, "right")?;
                let (other, none) = if self.src(right) == "None" {
                    (left, right)
                } else {
                    (right, left)
                };
                (self.src(none) == "None").then_some(())?;
                let inner = self.rust_type(other)?;
                Some(fill(&self.maps.union_none, &[("0", &inner)]))
            }
            "generic_type" | "subscript" => {
                let parts = named(node);
                let base = self.src(*parts.first()?);
                let template = self.maps.generic_types.get(base)?;
                let arguments: Vec<Node<'_>> = parts[1..]
                    .iter()
                    .flat_map(|n| {
                        if n.kind() == "type_parameter" {
                            named(*n)
                        } else {
                            vec![*n]
                        }
                    })
                    .collect();
                let arguments: Vec<String> = arguments
                    .into_iter()
                    .map(|a| self.rust_type(a))
                    .collect::<Option<_>>()?;
                let values: Vec<(String, String)> = arguments
                    .iter()
                    .enumerate()
                    .map(|(i, a)| (i.to_string(), a.clone()))
                    .collect();
                let refs: Vec<(&str, &str)> = values
                    .iter()
                    .map(|(k, v)| (k.as_str(), v.as_str()))
                    .collect();
                let filled = std::panic::catch_unwind(|| fill(template, &refs)).ok()?;
                if filled.starts_with("BTreeMap<") && !filled.starts_with("BTreeMap<String,") {
                    return None;
                }
                Some(filled)
            }
            _ => None,
        }
    }

    fn bind_parameters(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let Some(parameters) = field(node, "parameters") else {
            return String::new();
        };
        let mut out = String::new();
        let mut index = 0usize;
        let mut keyword_only = false;
        let mut declared = Vec::new();
        for p in named(parameters) {
            let (name, default) = match p.kind() {
                "identifier" => (self.src(p).to_owned(), None),
                "typed_parameter" => {
                    let inner = named(p).into_iter().next().expect("name");
                    match inner.kind() {
                        "list_splat_pattern" | "dictionary_splat_pattern" => {
                            let star = self.src(named(inner)[0]).to_owned();
                            self.splat(inner.kind(), &star, index, &declared, scope, &mut out);
                            if inner.kind() == "list_splat_pattern" {
                                keyword_only = true;
                            }
                            continue;
                        }
                        _ => (self.src(inner).to_owned(), None),
                    }
                }
                "default_parameter" | "typed_default_parameter" => {
                    let name = self.src(field(p, "name").expect("name")).to_owned();
                    let mut module = self.module_scope();
                    let value = self.expression(field(p, "value").expect("default"), &mut module);
                    (name, Some(value))
                }
                "list_splat_pattern" | "dictionary_splat_pattern" => {
                    let star = self.src(named(p)[0]).to_owned();
                    self.splat(p.kind(), &star, index, &declared, scope, &mut out);
                    if p.kind() == "list_splat_pattern" {
                        keyword_only = true;
                    }
                    continue;
                }
                "keyword_separator" => {
                    keyword_only = true;
                    continue;
                }
                _ => continue,
            };
            let position = if keyword_only {
                usize::MAX.to_string()
            } else {
                index.to_string()
            };
            let default = default.map_or_else(|| "None".into(), |d| format!("Some({d})"));
            out.push_str(&fill(
                &self.maps.declarations["parameter"],
                &[
                    ("local", &ident(&name)),
                    ("index", &position),
                    ("name", &name),
                    ("default", &default),
                ],
            ));
            out.push('\n');
            declared.push(name.clone());
            scope.locals.insert(name);
            if !keyword_only {
                index += 1;
            }
        }
        out
    }

    fn splat(
        &mut self,
        kind: &str,
        name: &str,
        index: usize,
        declared: &[String],
        scope: &mut Scope,
        out: &mut String,
    ) {
        let line = if kind == "list_splat_pattern" {
            fill(
                &self.maps.declarations["parameter *args"],
                &[("local", &ident(name)), ("index", &index.to_string())],
            )
        } else {
            let names = declared
                .iter()
                .map(|d| format!("{d:?}"))
                .collect::<Vec<_>>()
                .join(", ");
            fill(
                &self.maps.declarations["parameter **kwargs"],
                &[("local", &ident(name)), ("declared", &names)],
            )
        };
        out.push_str(&line);
        out.push('\n');
        scope.locals.insert(name.to_owned());
    }

    // ------------------------------------------------------- test cases

    fn test_wrappers(
        &mut self,
        node: Node<'_>,
        decorators: &[Node<'_>],
        name: &str,
        qualified: &str,
        rust_name: &str,
        class: Option<&str>,
    ) -> usize {
        let mut scope = self.module_scope();
        scope.function = qualified.to_owned();
        let mut rows: Vec<Vec<(String, String)>> = vec![Vec::new()];
        let mut setup = String::new();
        let mut patches: Vec<String> = Vec::new();
        for decorator in decorators {
            let mark = self.decorator_name(*decorator);
            let effect = self.maps.marks.get(&mark).map(String::as_str).unwrap_or(
                if mark.starts_with("pytest.mark.") {
                    "none"
                } else {
                    "runtime"
                },
            );
            let expression = named(*decorator).into_iter().next().expect("decorator");
            match effect {
                "none" | "fixture" => {}
                "parametrize" => match self.parametrize(expression, &mut scope) {
                    Some(new_rows) => {
                        rows = rows
                            .iter()
                            .flat_map(|row| {
                                new_rows.iter().map(move |n| {
                                    row.iter().cloned().chain(n.iter().cloned()).collect()
                                })
                            })
                            .collect();
                    }
                    None => {
                        let p = self.pending(qualified, *decorator, "parametrize");
                        setup.push_str(&format!("{p};\n"));
                    }
                },
                "skipif" | "skip" => {
                    let arguments = field(expression, "arguments")
                        .map(named)
                        .unwrap_or_default();
                    let condition = arguments
                        .iter()
                        .find(|a| a.kind() != "keyword_argument")
                        .copied();
                    let condition = match (effect, condition) {
                        ("skip", _) => "Object::Bool(true)".into(),
                        (_, Some(c)) => self.expression(c, &mut scope),
                        (_, None) => "Object::Bool(true)".into(),
                    };
                    let reason = self
                        .decorator_keyword(*decorator, "reason")
                        .unwrap_or_default();
                    setup.push_str(&format!(
                        "if object::truthy(&{condition}) {{ return doubles::skip(Args::new(vec![object::str_({reason:?})], vec![])); }}\n"
                    ));
                }
                "usefixtures" => {
                    for argument in field(expression, "arguments")
                        .map(named)
                        .unwrap_or_default()
                    {
                        let fixture = self
                            .src(argument)
                            .trim_matches(|c| c == '"' || c == '\'')
                            .to_owned();
                        let mut resolved = Vec::new();
                        let value = self.resolve_fixture(
                            &fixture,
                            class,
                            &mut resolved,
                            &mut setup,
                            qualified,
                            node,
                        );
                        setup.push_str(&format!("let _ = {value};\n"));
                    }
                }
                "patch" => {
                    // The decorator nearest the function gives the first argument.
                    let value = self.expression(expression, &mut scope);
                    patches.insert(0, value);
                }
                "runtime" => {
                    let value = self.expression(expression, &mut scope);
                    setup.push_str(&format!("doubles::apply_decorator(&{value});\n"));
                }
                _ => {
                    let p = self.pending(qualified, *decorator, "decorator");
                    setup.push_str(&format!("{p};\n"));
                }
            }
        }
        let mut count = 0;
        let mut parameters = self.parameter_names(node);
        let receiver = usize::from(parameters.first().is_some_and(|p| p == "self"));
        let injected: Vec<String> = parameters
            .drain(receiver..(receiver + patches.len()).min(parameters.len()))
            .collect();
        for (row_index, row) in rows.iter().enumerate() {
            let mut case_setup = setup.clone();
            let mut resolved: Vec<String> = Vec::new();
            for (fixture, f) in self.fixtures.clone() {
                if f.autouse && (f.method_of.is_none() || f.method_of.as_deref() == class) {
                    let value = self.resolve_fixture(
                        &fixture,
                        class,
                        &mut resolved,
                        &mut case_setup,
                        qualified,
                        node,
                    );
                    case_setup.push_str(&format!("let _ = {value};\n"));
                }
            }
            let mut keywords = Vec::new();
            let mut positional = Vec::new();
            for (parameter, value) in injected.iter().zip(&patches) {
                keywords.push(format!("({parameter:?}, doubles::apply_patch(&{value}))"));
            }
            for parameter in &parameters {
                if parameter == "self" {
                    positional.push(format!(
                        "object::call(&__class_{}(), Args::none())",
                        class.unwrap_or("")
                    ));
                    continue;
                }
                let value = match row.iter().find(|(n, _)| n == parameter) {
                    Some((_, v)) => v.clone(),
                    None => self.resolve_fixture(
                        parameter,
                        class,
                        &mut resolved,
                        &mut case_setup,
                        qualified,
                        node,
                    ),
                };
                keywords.push(format!("({parameter:?}, {value})"));
            }
            let call = format!(
                "{rust_name}(Args::new(vec![{}], vec![{}]))",
                positional.join(", "),
                keywords.join(", ")
            );
            let suffix = if rows.len() > 1 {
                format!("__row{row_index}")
            } else {
                String::new()
            };
            let case_name = format!("case_{}{suffix}", qualified.replace('.', "__"));
            let label = format!("{}::{qualified}{suffix}", self.rel);
            self.cases.push(fill(
                &self.maps.declarations["test wrapper"],
                &[
                    ("name", &ident(&case_name)),
                    ("label", &label),
                    ("setup", &indent(&indent(&case_setup))),
                    ("call", &call),
                ],
            ));
            count += 1;
        }
        let _ = name;
        count
    }

    fn resolve_fixture(
        &mut self,
        fixture: &str,
        class: Option<&str>,
        resolved: &mut Vec<String>,
        setup: &mut String,
        qualified: &str,
        node: Node<'_>,
    ) -> String {
        let variable = format!("__fixture_{}", fixture);
        if resolved.contains(&fixture.to_owned()) {
            return format!("{variable}.clone()");
        }
        let local = self
            .fixtures
            .get(fixture)
            .cloned()
            .filter(|f| f.method_of.is_none() || f.method_of.as_deref() == class);
        let local = if local.is_none() && !self.maps.fixtures.contains_key(fixture) {
            self.conftest_fixture(fixture)
        } else {
            local
        };
        let value = if let Some(builtin) = self.maps.fixtures.get(fixture) {
            builtin.clone()
        } else if let Some(f) = local {
            let mut keywords = Vec::new();
            for dependency in &f.parameters {
                let value =
                    self.resolve_fixture(dependency, class, resolved, setup, qualified, node);
                keywords.push(format!("({dependency:?}, {value})"));
            }
            let receiver = match &f.method_of {
                Some(c) => format!("object::call(&__class_{c}(), Args::none())"),
                None => String::new(),
            };
            format!(
                "object::fixture({}(Args::new(vec![{receiver}], vec![{}])))",
                f.function,
                keywords.join(", ")
            )
        } else {
            self.pending(qualified, node, &format!("fixture {fixture}"))
        };
        setup.push_str(&format!("let {variable} = {value};\n"));
        resolved.push(fixture.to_owned());
        format!("{variable}.clone()")
    }

    /// `parametrize("a,b", [(1, 2), ...])` → one binding list per row.
    fn parametrize(
        &mut self,
        call: Node<'_>,
        scope: &mut Scope,
    ) -> Option<Vec<Vec<(String, String)>>> {
        let arguments = named(field(call, "arguments")?);
        let positional: Vec<Node<'_>> = arguments
            .iter()
            .copied()
            .filter(|a| a.kind() != "keyword_argument")
            .collect();
        let (names, values) = (positional.first()?, positional.get(1)?);
        let names: Vec<String> = match names.kind() {
            "string" => self
                .src(*names)
                .trim_matches(|c| c == '"' || c == '\'')
                .split(',')
                .map(|s| s.trim().to_owned())
                .collect(),
            "tuple" | "list" => named(*names)
                .iter()
                .map(|n| {
                    self.src(*n)
                        .trim_matches(|c| c == '"' || c == '\'')
                        .to_owned()
                })
                .collect(),
            _ => return None,
        };
        if !matches!(values.kind(), "list" | "tuple") {
            return None;
        }
        let mut rows = Vec::new();
        for row in named(*values) {
            let row = if row.kind() == "call"
                && self.canonical(self.src(field(row, "function")?), &scope.aliases)
                    == "pytest.param"
            {
                let parts: Vec<Node<'_>> = named(field(row, "arguments")?)
                    .into_iter()
                    .filter(|a| a.kind() != "keyword_argument")
                    .collect();
                parts
            } else if names.len() == 1 {
                vec![row]
            } else if matches!(row.kind(), "tuple" | "list") {
                named(row)
            } else {
                return None;
            };
            if row.len() != names.len() {
                return None;
            }
            let bound = names
                .iter()
                .cloned()
                .zip(row.into_iter().map(|v| self.expression(v, scope)))
                .collect();
            rows.push(bound);
        }
        Some(rows)
    }

    // ------------------------------------------------------ statements

    fn block(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let mut out = String::new();
        for statement in named(node) {
            out.push_str(&self.statement(statement, scope));
            if !out.ends_with('\n') {
                out.push('\n');
            }
        }
        out
    }

    fn statement(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let s = &self.maps.statements;
        match node.kind() {
            // The shared cell was declared by the enclosing function.
            "nonlocal_statement" => String::new(),
            "expression_statement" => {
                let inner = named(node).into_iter().next().expect("expression");
                match inner.kind() {
                    "assignment" => self.assignment(inner, scope),
                    "augmented_assignment" => self.augmented(inner, scope),
                    "string" => String::new(),
                    _ => {
                        let value = self.expression(inner, scope);
                        fill(&s["expression"], &[("value", &value)])
                    }
                }
            }
            "return_statement" => {
                let value = named(node)
                    .into_iter()
                    .next()
                    .map_or_else(|| "Object::None".into(), |v| self.expression(v, scope));
                self.return_value(&value, scope, node) + "\n"
            }
            "pass_statement" => fill(&s["pass"], &[]),
            "break_statement" => {
                if scope.in_block && !scope.loop_in_block {
                    self.maps.returns["break inside block"].clone()
                } else {
                    self.break_code(scope) + ";"
                }
            }
            "continue_statement" => {
                if scope.in_block && !scope.loop_in_block {
                    self.maps.returns["continue inside block"].clone()
                } else {
                    "continue;".into()
                }
            }
            "if_statement" => self.if_statement(node, scope),
            "while_statement" | "for_statement" => {
                let flag = field(node, "alternative").map(|_| self.fresh("completed"));
                let code = if node.kind() == "while_statement" {
                    let condition =
                        self.expression(field(node, "condition").expect("condition"), scope);
                    let body = self.looped(field(node, "body").expect("body"), scope, flag.clone());
                    fill(
                        &s["while"],
                        &[("condition", &condition), ("body", &indent(&body))],
                    )
                } else {
                    let iterable = self.expression(field(node, "right").expect("iterable"), scope);
                    let item = self.fresh("item");
                    let target =
                        self.bind_target(field(node, "left").expect("target"), &item, scope);
                    let body = self.looped(field(node, "body").expect("body"), scope, flag.clone());
                    fill(
                        &s["for"],
                        &[
                            ("item", &item),
                            ("iterable", &iterable),
                            ("target", &indent(&target)),
                            ("body", &indent(&body)),
                        ],
                    )
                };
                match (flag, field(node, "alternative")) {
                    (Some(flag), Some(alternative)) => {
                        let body = named(alternative)
                            .into_iter()
                            .find(|n| n.kind() == "block")
                            .expect("else body");
                        let otherwise = self.block(body, scope);
                        fill(
                            &s["loop else"],
                            &[
                                ("flag", &flag),
                                ("loop", &code),
                                ("otherwise", &indent(&otherwise)),
                            ],
                        )
                    }
                    _ => code,
                }
            }
            "with_statement" => self.with_statement(node, scope),
            "try_statement" => self.try_statement(node, scope),
            "assert_statement" => {
                let parts = named(node);
                let test = self.expression(parts[0], scope);
                let message = parts.get(1).map_or_else(
                    || "None".into(),
                    |m| format!("Some({})", self.expression(*m, scope)),
                );
                fill(
                    &s["assert"],
                    &[
                        ("test", &test),
                        ("loc", &self.loc(node)),
                        ("message", &message),
                    ],
                )
            }
            "raise_statement" => match named(node).first() {
                Some(exception) => {
                    let exception = self.expression(*exception, scope);
                    fill(&s["raise"], &[("exception", &exception)])
                }
                None if scope.in_handler => s["bare raise"].clone(),
                None => self.pending(&scope.function, node, "bare raise") + ";",
            },
            "import_statement" | "import_from_statement" => {
                let mut aliases = std::mem::take(&mut scope.aliases);
                self.import(node, &mut aliases);
                scope.aliases = aliases;
                String::new()
            }
            "class_definition" => self.nested_class(node, scope),
            "function_definition" | "decorated_definition" => self.nested_function(node, scope),
            "delete_statement" => {
                let mut out = String::new();
                for target in named(node).into_iter().flat_map(|t| {
                    if t.kind() == "expression_list" {
                        named(t)
                    } else {
                        vec![t]
                    }
                }) {
                    if target.kind() == "subscript" {
                        let value = self.expression(field(target, "value").expect("value"), scope);
                        let key = self.subscript_key(target, scope);
                        out.push_str(&format!("object::delitem(&{value}, &{key});\n"));
                    } else {
                        out.push_str(&(self.pending(&scope.function, target, "del") + ";\n"));
                    }
                }
                out
            }
            other => self.pending(&scope.function, node, other) + ";",
        }
    }

    fn return_value(&mut self, value: &str, scope: &Scope, node: Node<'_>) -> String {
        if scope.in_block {
            fill(&self.maps.returns["inside block"], &[("value", value)])
        } else if let Some(ty) = &scope.typed_return {
            fill(
                &self.maps.returns["typed"],
                &[("type", ty), ("value", value), ("loc", &self.loc(node))],
            )
        } else {
            fill(&self.maps.returns["dynamic"], &[("value", value)])
        }
    }

    fn looped(&mut self, body: Node<'_>, scope: &mut Scope, flag: Option<String>) -> String {
        let saved = (scope.in_loop, scope.loop_in_block, scope.loop_flag.clone());
        scope.in_loop = true;
        scope.loop_in_block = scope.in_block;
        scope.loop_flag = flag;
        let code = self.block(body, scope);
        (scope.in_loop, scope.loop_in_block, scope.loop_flag) = saved;
        code
    }

    /// `break`, clearing the completion flag of a loop with `else`.
    fn break_code(&self, scope: &Scope) -> String {
        match &scope.loop_flag {
            Some(flag) => fill(&self.maps.statements["break with else"], &[("flag", flag)]),
            None => "break".into(),
        }
    }

    fn if_statement(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let s = self.maps.statements.clone();
        let condition = self.expression(field(node, "condition").expect("condition"), scope);
        let body = self.block(field(node, "consequence").expect("consequence"), scope);
        let mut alternatives = String::new();
        let mut cursor = node.walk();
        let clauses: Vec<Node<'_>> = node
            .children_by_field_name("alternative", &mut cursor)
            .collect();
        for clause in clauses {
            match clause.kind() {
                "elif_clause" => {
                    let c = self.expression(field(clause, "condition").expect("condition"), scope);
                    let b = self.block(field(clause, "consequence").expect("consequence"), scope);
                    alternatives.push_str(&fill(
                        &s["elif"],
                        &[("condition", &c), ("body", &indent(&b))],
                    ));
                }
                "else_clause" => {
                    let b = self.block(field(clause, "body").expect("body"), scope);
                    alternatives.push_str(&fill(&s["else"], &[("body", &indent(&b))]));
                }
                _ => {}
            }
        }
        fill(
            &s["if"],
            &[
                ("condition", &condition),
                ("body", &indent(&body)),
                ("alternatives", &alternatives),
            ],
        )
    }

    /// Run `build` as a block closure, then let its `Flow` leave the closure.
    fn block_scope(&mut self, scope: &Scope) -> Scope {
        let mut inner = scope.clone();
        inner.in_block = true;
        inner.loop_in_block = false;
        inner
    }

    fn propagate(&self, scope: &Scope) -> String {
        let (return_value, break_, continue_) = if scope.in_block {
            (
                "return Flow::Return(__v)".to_owned(),
                "return Flow::Break".to_owned(),
                "return Flow::Continue".to_owned(),
            )
        } else {
            let ret = match &scope.typed_return {
                Some(ty) => format!("return convert::to::<{ty}>(&__v, \"return\")"),
                None => "return __v".into(),
            };
            let (b, c) = if scope.in_loop {
                (self.break_code(scope), "continue")
            } else {
                ("{}".to_owned(), "{}")
            };
            (ret, b, c.to_owned())
        };
        fill(
            &self.maps.statements["propagate"],
            &[
                ("return_value", &return_value),
                ("break_", &break_),
                ("continue_", &continue_),
            ],
        )
    }

    fn with_statement(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let clause = named(node)
            .into_iter()
            .find(|n| n.kind() == "with_clause")
            .expect("with clause");
        let items: Vec<Node<'_>> = named(clause)
            .into_iter()
            .filter(|n| n.kind() == "with_item")
            .collect();
        let body = field(node, "body").expect("with body");
        self.with_items(&items, body, scope)
    }

    fn with_items(&mut self, items: &[Node<'_>], body: Node<'_>, scope: &mut Scope) -> String {
        let Some((item, rest)) = items.split_first() else {
            return self.block(body, scope);
        };
        let value = field(*item, "value").expect("with value");
        let (manager, alias) = if value.kind() == "as_pattern" {
            let parts = named(value);
            let alias = named(*parts.last().expect("alias")).into_iter().next();
            (parts[0], alias)
        } else {
            (value, None)
        };
        let manager_code = self.expression(manager, scope);
        let mut inner = self.block_scope(scope);
        let entered = self.fresh("entered");
        let target = match alias {
            Some(alias) => self.bind_target(alias, &entered, &mut inner),
            None => String::new(),
        };
        let code = self.with_items(rest, body, &mut inner);
        let manager_var = self.fresh("manager");
        let template = &self.maps.statements["with"];
        let filled = fill(
            template,
            &[
                ("entered", &entered),
                ("manager", &manager_var),
                ("target", &indent(&target)),
                ("body", &indent(&code)),
                ("propagate", &self.propagate(scope)),
            ],
        );
        format!("let {manager_var} = {manager_code};\n{filled}\n")
    }

    fn try_statement(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let body = field(node, "body").expect("try body");
        let parts = named(node);
        let handlers: Vec<Node<'_>> = parts
            .iter()
            .copied()
            .filter(|n| n.kind() == "except_clause")
            .collect();
        let orelse = parts.iter().copied().find(|n| n.kind() == "else_clause");
        let finally = parts.iter().copied().find(|n| n.kind() == "finally_clause");
        if parts.iter().any(|n| n.kind() == "except_group_clause") {
            return self.pending(&scope.function, node, "except*") + ";";
        }
        let build = |this: &mut Self, scope: &mut Scope| -> String {
            let mut inner = this.block_scope(scope);
            let code = this.block(body, &mut inner);
            let mut handler_code = String::new();
            for handler in &handlers {
                let handler_parts = named(*handler);
                let block = handler_parts
                    .iter()
                    .copied()
                    .find(|n| n.kind() == "block")
                    .expect("handler body");
                let head: Vec<Node<'_>> = handler_parts
                    .iter()
                    .copied()
                    .filter(|n| n.kind() != "block")
                    .collect();
                let (kind, alias) = match head.first() {
                    None => ("object::name(\"builtins.BaseException\")".to_owned(), None),
                    Some(h) if h.kind() == "as_pattern" => {
                        let p = named(*h);
                        let alias = named(*p.last().expect("alias")).into_iter().next();
                        (this.expression(p[0], scope), alias)
                    }
                    Some(h) => (this.expression(*h, scope), head.get(1).copied()),
                };
                let mut handler_scope = scope.clone();
                handler_scope.in_handler = true;
                let bind = match alias {
                    Some(a) => this.bind_target(a, "__raised.0.clone()", &mut handler_scope),
                    None => String::new(),
                };
                let handler_body = this.block(block, &mut handler_scope);
                handler_code.push_str(&fill(
                    &this.maps.statements["except"],
                    &[
                        ("kind", &kind),
                        ("bind", &indent(&bind)),
                        ("body", &indent(&handler_body)),
                    ],
                ));
            }
            handler_code.push_str(
                &this.maps.statements["except fallback"]
                    .replace("{{", "{")
                    .replace("}}", "}"),
            );
            let orelse_code = match orelse {
                Some(o) => {
                    let b = this.block(field(o, "body").expect("else body"), scope);
                    format!("if let Flow::Normal = __flow {{\n{}}}\n", indent(&b))
                }
                None => String::new(),
            };
            if handlers.is_empty() {
                format!(
                    "let __flow = match object::catch(|| {{\n{}Flow::Normal\n}}) {{ Ok(__flow) => __flow, Err(__raised) => object::reraise(__raised) }};\n{orelse_code}{}\n",
                    indent(&code),
                    this.propagate(scope)
                )
            } else {
                fill(
                    &this.maps.statements["try"],
                    &[
                        ("body", &indent(&code)),
                        ("handlers", &indent(&indent(&handler_code))),
                        ("orelse", &orelse_code),
                        ("propagate", &this.propagate(scope)),
                    ],
                ) + "\n"
            }
        };
        match finally {
            None => build(self, scope),
            Some(f) => {
                let mut inner = self.block_scope(scope);
                let inner_code = build(self, &mut inner);
                let finally_code = self.block(
                    named(f)
                        .into_iter()
                        .find(|n| n.kind() == "block")
                        .expect("finally body"),
                    scope,
                );
                fill(
                    &self.maps.statements["finally"],
                    &[
                        ("inner", &indent(&inner_code)),
                        ("finally", &finally_code),
                        ("propagate", &self.propagate(scope)),
                    ],
                ) + "\n"
            }
        }
    }

    fn nested_function(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let (decorators, definition) = unwrap_decorated(node);
        if definition.kind() != "function_definition" {
            return self.pending(&scope.function, node, "decorated nested def") + ";";
        }
        let name = self
            .src(field(definition, "name").expect("name"))
            .to_owned();
        let mut value = self.nested_value(definition, scope);
        // `@a @b def f` is `f = a(b(f))`.
        for decorator in decorators.iter().rev() {
            let expression = named(*decorator).into_iter().next().expect("decorator");
            let callee = self.expression(expression, scope);
            value = fill(
                &self.maps.declarations["decorator"],
                &[("decorator", &callee), ("value", &value)],
            );
        }
        scope.locals.insert(name.clone());
        format!("{} = {};", ident(&name), value)
    }

    /// A function defined inside another, as a closure value.
    fn nested_value(&mut self, definition: Node<'_>, scope: &mut Scope) -> String {
        let name = self
            .src(field(definition, "name").expect("name"))
            .to_owned();
        let is_async = children(definition)
            .first()
            .is_some_and(|c| self.src(*c) == "async");
        let body = field(definition, "body").expect("body");
        let mut inner = scope.clone();
        inner.function = format!("{}.{name}", scope.function);
        inner.in_block = false;
        inner.loop_in_block = false;
        inner.in_loop = false;
        inner.typed_return = None;
        inner.in_handler = false;
        if let Some(class) = inner.defining_class.take() {
            inner.owner = self
                .parameter_names(definition)
                .into_iter()
                .next()
                .map(|p| (class, p));
        }
        let captures = self.captures(definition, scope);
        let binds = self.bind_parameters(definition, &mut inner);
        let mut parameters: BTreeSet<String> =
            self.parameter_names(definition).into_iter().collect();
        if let Some(list) = field(definition, "parameters") {
            for p in named(list) {
                if matches!(p.kind(), "list_splat_pattern" | "dictionary_splat_pattern") {
                    parameters.extend(named(p).into_iter().map(|n| self.src(n).to_owned()));
                }
            }
        }
        let own_nonlocals = nonlocal_names(body, self.src, false);
        let mut declare = String::new();
        // A name assigned in the nested body is its own local, unless `nonlocal`.
        for local in collect_locals(body, self.src) {
            if own_nonlocals.contains(&local) || parameters.contains(&local) {
                continue;
            }
            inner.locals.insert(local.clone());
            declare.push_str(&fill(
                &self.maps.declarations["local"],
                &[("local", &ident(&local))],
            ));
            declare.push('\n');
        }
        let own_locals: BTreeSet<String> = collect_locals(body, self.src).into_iter().collect();
        inner
            .cells
            .retain(|c| own_nonlocals.contains(c) || !own_locals.contains(c));
        declare.push_str(&self.declare_cells(body, &mut inner));
        let yields = contains_kind(body, "yield");
        inner.generator = yields;
        let statements = self.block(body, &mut inner);
        let body_code = if yields || is_async {
            let kind = if yields {
                "generator body"
            } else {
                "coroutine body"
            };
            fill(
                &self.maps.declarations[kind],
                &[("body", &format!("{declare}{statements}"))],
            )
        } else {
            format!("{declare}{statements}Object::None")
        };
        let inner_captures = self.inner_captures(definition, scope);
        fill(
            &self.maps.declarations["nested def"],
            &[
                ("qualname", &name),
                ("captures", &captures),
                ("inner_captures", &inner_captures),
                ("binds", &binds),
                ("body", &body_code),
            ],
        )
    }

    /// Locals that a nested function rebinds with `nonlocal` become cells.
    fn declare_cells(&self, body: Node<'_>, scope: &mut Scope) -> String {
        let mut out = String::new();
        for name in nonlocal_names(body, self.src, true) {
            if scope.locals.contains(&name) && scope.cells.insert(name.clone()) {
                out.push_str(&fill(
                    &self.maps.declarations["cell"],
                    &[("local", &ident(&name))],
                ));
                out.push('\n');
            }
        }
        out
    }

    /// Inside a reusable closure, shadow each capture with a clone, so inner
    /// `move` closures (coroutines, generators) can take it.
    fn inner_captures(&self, node: Node<'_>, scope: &Scope) -> String {
        self.captures(node, scope)
    }

    /// Clone the outer locals a closure reads, so it can own them.
    fn captures(&self, node: Node<'_>, scope: &Scope) -> String {
        let mut names = BTreeSet::new();
        collect_identifiers(node, self.src, &mut names);
        names
            .into_iter()
            .filter(|n| scope.locals.contains(n))
            .map(|n| format!("let {0} = {0}.clone(); ", ident(&n)))
            .collect()
    }

    fn bind_target(&mut self, target: Node<'_>, value: &str, scope: &mut Scope) -> String {
        match target.kind() {
            "identifier" => {
                let name = self.src(target).to_owned();
                if scope.cells.contains(&name) {
                    return fill(
                        &self.maps.declarations["cell write"],
                        &[("local", &ident(&name)), ("value", value)],
                    );
                }
                scope.locals.insert(name.clone());
                format!("{} = {value};", ident(&name))
            }
            "attribute" => {
                let object = self.expression(field(target, "object").expect("object"), scope);
                let attribute = self.src(field(target, "attribute").expect("attribute"));
                format!("object::setattr(&{object}, {attribute:?}, {value});")
            }
            "subscript" => {
                let object = self.expression(field(target, "value").expect("value"), scope);
                let key = self.subscript_key(target, scope);
                format!("object::setitem(&{object}, &{key}, {value});")
            }
            "pattern_list" | "tuple_pattern" | "list_pattern" | "tuple" | "list"
            | "expression_list" => {
                let parts = named(target);
                if parts.iter().any(|p| p.kind() == "list_splat_pattern") {
                    return self.pending(&scope.function, target, "starred target") + ";";
                }
                let values = self.fresh("values");
                let mut out = format!("let {values} = object::unpack(&{value}, {});", parts.len());
                for (i, part) in parts.iter().enumerate() {
                    out.push(' ');
                    out.push_str(&self.bind_target(
                        *part,
                        &format!("{values}[{i}].clone()"),
                        scope,
                    ));
                }
                out
            }
            "parenthesized_expression" => {
                let inner = named(target)[0];
                self.bind_target(inner, value, scope)
            }
            _ => self.pending(&scope.function, target, "assignment target") + ";",
        }
    }

    fn assignment(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let Some(right) = field(node, "right") else {
            return String::new();
        };
        let mut targets = vec![field(node, "left").expect("left")];
        let mut value_node = right;
        while value_node.kind() == "assignment" {
            targets.push(field(value_node, "left").expect("left"));
            match field(value_node, "right") {
                Some(r) => value_node = r,
                None => return String::new(),
            }
        }
        let value = self.expression(value_node, scope);
        if targets.len() == 1 {
            let target = self.bind_target(targets[0], &value, scope);
            return fill(&self.maps.statements["pass"], &[]) + &target;
        }
        let temporary = self.fresh("value");
        let mut out = format!("let {temporary} = {value};");
        for target in targets {
            out.push(' ');
            out.push_str(&self.bind_target(target, &format!("{temporary}.clone()"), scope));
        }
        out
    }

    fn augmented(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let left = field(node, "left").expect("left");
        let operator = self.src(field(node, "operator").expect("operator"));
        let Some(function) = self.maps.augmented.get(operator).cloned() else {
            return self.pending(&scope.function, node, "augmented operator") + ";";
        };
        let current = self.expression(left, scope);
        let right = self.expression(field(node, "right").expect("right"), scope);
        let value = format!("{function}(&{current}, &{right})");
        self.bind_target(left, &value, scope)
    }

    // ----------------------------------------------------- expressions

    fn expression(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let l = &self.maps.literals;
        match node.kind() {
            "none" => l["None"].clone(),
            "true" => l["True"].clone(),
            "false" => l["False"].clone(),
            "ellipsis" => l["..."].clone(),
            "integer" => match parse_int(self.src(node)) {
                Some(i) => fill(&l["int"], &[("0", &i.to_string())]),
                None => self.pending(&scope.function, node, "integer"),
            },
            "float" => match self.src(node).replace('_', "").parse::<f64>() {
                Ok(f) => fill(&l["float"], &[("0", &format!("{f:?}"))]),
                Err(_) => self.pending(&scope.function, node, "float"),
            },
            "string" | "concatenated_string" => self.string(node, scope),
            "identifier" => self.identifier(node, scope),
            "attribute" => self.attribute(node, scope),
            "call" => self.call(node, scope),
            "parenthesized_expression" => match named(node).first() {
                Some(inner) => self.expression(*inner, scope),
                None => l["tuple"].replace("{0}", ""),
            },
            "list" | "tuple" | "set" | "expression_list" | "pattern_list" => {
                let parts = named(node);
                if parts.iter().any(|p| p.kind() == "list_splat") {
                    return self.splatted_sequence(node, &parts, scope);
                }
                let items: Vec<String> = parts.iter().map(|p| self.expression(*p, scope)).collect();
                let key = match node.kind() {
                    "list" => "list",
                    "set" => "set",
                    _ => "tuple",
                };
                fill(&l[key], &[("0", &items.join(", "))])
            }
            "dictionary" => self.dictionary(node, scope),
            "binary_operator" => {
                let operator = self.src(field(node, "operator").expect("operator"));
                match self.maps.binary.get(operator).cloned() {
                    Some(function) => {
                        let left = self.expression(field(node, "left").expect("left"), scope);
                        let right = self.expression(field(node, "right").expect("right"), scope);
                        format!("{function}(&{left}, &{right})")
                    }
                    None => self.pending(&scope.function, node, &format!("operator {operator}")),
                }
            }
            "unary_operator" => {
                let operator = self.src(field(node, "operator").expect("operator"));
                let argument = self.expression(field(node, "argument").expect("argument"), scope);
                match operator {
                    "+" => argument,
                    _ => match self.maps.unary.get(operator) {
                        Some(function) => format!("{function}(&{argument})"),
                        None => {
                            self.pending(&scope.function, node, &format!("operator {operator}"))
                        }
                    },
                }
            }
            "not_operator" => {
                let argument = self.expression(field(node, "argument").expect("argument"), scope);
                format!("{}(&{argument})", self.maps.unary["not"])
            }
            "boolean_operator" => {
                let operator = self
                    .src(field(node, "operator").expect("operator"))
                    .to_owned();
                let left = self.expression(field(node, "left").expect("left"), scope);
                let right = self.expression(field(node, "right").expect("right"), scope);
                fill(
                    &self.maps.operators[&operator],
                    &[("left", &left), ("right", &right)],
                )
            }
            "comparison_operator" => self.comparison(node, scope),
            "conditional_expression" => {
                let parts = named(node);
                let then = self.expression(parts[0], scope);
                let condition = self.expression(parts[1], scope);
                let other = self.expression(parts[2], scope);
                fill(
                    &self.maps.operators["conditional"],
                    &[
                        ("condition", &condition),
                        ("then", &then),
                        ("other", &other),
                    ],
                )
            }
            "subscript" => {
                let value = self.expression(field(node, "value").expect("value"), scope);
                let mut cursor = node.walk();
                let keys: Vec<Node<'_>> = node
                    .children_by_field_name("subscript", &mut cursor)
                    .collect();
                if keys.len() == 1 && keys[0].kind() == "slice" {
                    return self.slice(&value, keys[0], scope);
                }
                let key = self.subscript_key(node, scope);
                format!("object::getitem(&{value}, &{key})")
            }
            "await" => {
                let inner = named(node)[0];
                format!("object::await_({})", self.expression(inner, scope))
            }
            "yield" if scope.generator => {
                let value = named(node).first().map_or_else(
                    || "Object::None".into(),
                    |v| {
                        let v = *v;
                        self.expression(v, scope)
                    },
                );
                if children(node).iter().any(|c| self.src(*c) == "from") {
                    return format!(
                        "{{ for __yielded in object::iter(&{value}) {{ __yield.send(__yielded); }} Object::None }}"
                    );
                }
                format!("__yield.send({value})")
            }
            "lambda" => self.lambda(node, scope),
            "list_comprehension"
            | "set_comprehension"
            | "generator_expression"
            | "dictionary_comprehension" => self.comprehension(node, scope),
            "keyword_argument" => self.expression(field(node, "value").expect("value"), scope),
            other => self.pending(&scope.function, node, other),
        }
    }

    fn splatted_sequence(
        &mut self,
        node: Node<'_>,
        parts: &[Node<'_>],
        scope: &mut Scope,
    ) -> String {
        let mut pushes = String::new();
        for part in parts {
            if part.kind() == "list_splat" {
                let inner = self.expression(named(*part)[0], scope);
                pushes.push_str(&format!("__items.extend(object::iter(&{inner})); "));
            } else {
                let value = self.expression(*part, scope);
                pushes.push_str(&format!("__items.push({value}); "));
            }
        }
        let constructor = if node.kind() == "list" {
            "object::list"
        } else if node.kind() == "set" {
            "object::set"
        } else {
            "object::tuple"
        };
        format!("{{ let mut __items = Vec::new(); {pushes}{constructor}(__items) }}")
    }

    fn identifier(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let name = self.src(node);
        if scope.cells.contains(name) {
            return fill(
                &self.maps.declarations["cell read"],
                &[("local", &ident(name)), ("name", name)],
            );
        }
        if scope.locals.contains(name) {
            return format!("object::local(&{}, {name:?})", ident(name));
        }
        if name == "__file__" {
            return self.file_literal(&self.source_file);
        }
        if let Some(canonical) = scope.aliases.get(name) {
            return format!("object::name({canonical:?})");
        }
        match self.items.get(name) {
            Some(Item::Function(rust)) => return format!("object::func({name:?}, {rust})"),
            Some(Item::Constant(rust)) => return format!("{rust}()"),
            None => {}
        }
        if PYTHON_BUILTINS.contains(&name) || self.maps.is_known(&format!("builtins.{name}")) {
            return format!("object::name(\"builtins.{name}\")");
        }
        format!("object::name({name:?})")
    }

    /// `a.b.c` with `a` an import resolves to the dotted name, so patches apply.
    fn dotted_name(&self, node: Node<'_>, scope: &Scope) -> Option<String> {
        let mut parts = Vec::new();
        let mut current = node;
        while current.kind() == "attribute" {
            parts.push(self.src(field(current, "attribute")?));
            current = field(current, "object")?;
        }
        if current.kind() != "identifier" {
            return None;
        }
        let root = self.src(current);
        if scope.locals.contains(root) {
            return None;
        }
        let base = scope.aliases.get(root).cloned()?;
        parts.reverse();
        Some(format!("{base}.{}", parts.join(".")))
    }

    fn attribute(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        if let Some(dotted) = self.dotted_name(node, scope) {
            return self.resolve_dotted(&dotted);
        }
        let object = self.expression(field(node, "object").expect("object"), scope);
        let attribute = self.src(field(node, "attribute").expect("attribute"));
        format!("object::getattr(&{object}, {attribute:?})")
    }

    /// A dotted name: itself when mapped, else the longest mapped prefix with
    /// the rest read as attributes (`os.environ.get`), else the plain name.
    fn resolve_dotted(&self, dotted: &str) -> String {
        if let Some(module) = dotted.strip_suffix(".__file__")
            && let Some(path) = self.module_file(module)
        {
            return self.file_literal(&path);
        }
        if self.maps.is_known(dotted) {
            return format!("object::name({dotted:?})");
        }
        let mut cut = dotted.len();
        while let Some(dot) = dotted[..cut].rfind('.') {
            let prefix = &dotted[..dot];
            if self.maps.is_known(prefix) {
                let mut code = format!("object::name({prefix:?})");
                for attribute in dotted[dot + 1..].split('.') {
                    code = format!("object::getattr(&{code}, {attribute:?})");
                }
                return code;
            }
            cut = dot;
        }
        format!("object::name({dotted:?})")
    }

    fn arguments(&mut self, arguments: Node<'_>, scope: &mut Scope) -> String {
        if arguments.kind() == "generator_expression" {
            let value = self.expression(arguments, scope);
            return format!("Args::new(vec![{value}], vec![])");
        }
        let mut positional = Vec::new();
        let mut keywords = Vec::new();
        let mut star = None;
        let mut double = None;
        for argument in named(arguments) {
            match argument.kind() {
                "keyword_argument" => {
                    let key = self.src(field(argument, "name").expect("name")).to_owned();
                    let value = self.expression(field(argument, "value").expect("value"), scope);
                    keywords.push(format!("({key:?}, {value})"));
                }
                "list_splat" => star = Some(self.expression(named(argument)[0], scope)),
                "dictionary_splat" => double = Some(self.expression(named(argument)[0], scope)),
                _ => positional.push(self.expression(argument, scope)),
            }
        }
        let mut out = format!(
            "Args::new(vec![{}], vec![{}])",
            positional.join(", "),
            keywords.join(", ")
        );
        if star.is_some() || double.is_some() {
            let wrap =
                |v: Option<String>| v.map_or_else(|| "None".into(), |v| format!("Some({v})"));
            out = format!("{out}.spread({}, {})", wrap(star), wrap(double));
        }
        out
    }

    fn call(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let callee = field(node, "function").expect("callee");
        let arguments = field(node, "arguments").expect("arguments");
        if callee.kind() == "identifier"
            && self.src(callee) == "super"
            && named(arguments).is_empty()
            && let Some((class, receiver)) = scope.owner.clone()
        {
            return fill(
                &self.maps.declarations["super"],
                &[
                    ("receiver", &ident(&receiver)),
                    ("class", &format!("{class:?}")),
                ],
            );
        }
        let args = self.arguments(arguments, scope);
        if callee.kind() == "identifier"
            && !scope.locals.contains(self.src(callee))
            && let Some(Item::Function(rust)) = self.items.get(self.src(callee))
        {
            return format!("{rust}({args})");
        }
        if callee.kind() == "attribute" && self.dotted_name(callee, scope).is_none() {
            let object = self.expression(field(callee, "object").expect("object"), scope);
            let method = self.src(field(callee, "attribute").expect("attribute"));
            return format!("object::call_method(&{object}, {method:?}, {args})");
        }
        let callee = self.expression(callee, scope);
        format!("object::call(&{callee}, {args})")
    }

    fn comparison(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let mut operands = Vec::new();
        let mut operators: Vec<String> = Vec::new();
        let mut pending_operator = String::new();
        for child in children(node) {
            if child.is_named() {
                if !pending_operator.is_empty() {
                    operators.push(std::mem::take(&mut pending_operator));
                }
                operands.push(child);
            } else {
                let token = self.src(child);
                if !pending_operator.is_empty() {
                    pending_operator.push(' ');
                }
                pending_operator.push_str(token);
            }
        }
        let mut functions = Vec::new();
        for operator in &operators {
            match self.maps.compare.get(operator) {
                Some(f) => functions.push(f.clone()),
                None => {
                    return self.pending(&scope.function, node, &format!("comparison {operator}"));
                }
            }
        }
        let values: Vec<String> = operands
            .iter()
            .map(|o| self.expression(*o, scope))
            .collect();
        if functions.len() == 1 {
            return format!("{}(&{}, &{})", functions[0], values[0], values[1]);
        }
        let mut code = String::from("{ ");
        for (i, value) in values.iter().enumerate() {
            code.push_str(&format!("let __c{i} = {value}; "));
        }
        let checks: Vec<String> = functions
            .iter()
            .enumerate()
            .map(|(i, f)| format!("object::truthy(&{f}(&__c{i}, &__c{}))", i + 1))
            .collect();
        code.push_str(&format!("Object::Bool({}) }}", checks.join(" && ")));
        code
    }

    fn subscript_key(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let mut cursor = node.walk();
        let keys: Vec<Node<'_>> = node
            .children_by_field_name("subscript", &mut cursor)
            .collect();
        if keys.len() == 1 {
            return self.expression(keys[0], scope);
        }
        let items: Vec<String> = keys.iter().map(|k| self.expression(*k, scope)).collect();
        fill(&self.maps.literals["tuple"], &[("0", &items.join(", "))])
    }

    fn slice(&mut self, value: &str, slice: Node<'_>, scope: &mut Scope) -> String {
        let mut bounds: Vec<Option<Node<'_>>> = vec![None];
        for child in children(slice) {
            if self.src(child) == ":" && !child.is_named() {
                bounds.push(None);
            } else if child.is_named() {
                *bounds.last_mut().expect("bound") = Some(child);
            }
        }
        if bounds.len() > 2 && bounds[2].is_some() {
            return self.pending(&scope.function, slice, "slice step");
        }
        let start = bounds[0].map_or_else(|| "Object::None".into(), |b| self.expression(b, scope));
        let stop = bounds
            .get(1)
            .copied()
            .flatten()
            .map_or_else(|| "Object::None".into(), |b| self.expression(b, scope));
        format!("object::slice(&{value}, &{start}, &{stop})")
    }

    fn dictionary(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let parts = named(node);
        let spread = parts.iter().any(|p| p.kind() == "dictionary_splat");
        let mut entries = Vec::new();
        for part in parts {
            match part.kind() {
                "pair" => {
                    let key = self.expression(field(part, "key").expect("key"), scope);
                    let value = self.expression(field(part, "value").expect("value"), scope);
                    entries.push(if spread {
                        format!("Ok(({key}, {value}))")
                    } else {
                        format!("({key}, {value})")
                    });
                }
                "dictionary_splat" => {
                    let value = self.expression(named(part)[0], scope);
                    entries.push(format!("Err({value})"));
                }
                _ => {}
            }
        }
        let key = if spread { "dict with spread" } else { "dict" };
        fill(&self.maps.literals[key], &[("0", &entries.join(", "))])
    }

    fn lambda(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let mut inner = scope.clone();
        inner.in_block = false;
        inner.typed_return = None;
        let captures = self.captures(node, scope);
        let binds = self.bind_parameters(node, &mut inner);
        let body = self.expression(field(node, "body").expect("body"), &mut inner);
        let inner_captures = self.inner_captures(node, scope);
        fill(
            &self.maps.declarations["lambda"],
            &[
                ("captures", &captures),
                ("inner_captures", &inner_captures),
                ("binds", &binds),
                ("body", &body),
            ],
        )
    }

    fn comprehension(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let parts = named(node);
        let element = parts[0];
        let mut inner = scope.clone();
        let mut opening = String::new();
        let mut closing = String::new();
        for clause in &parts[1..] {
            match clause.kind() {
                "for_in_clause" => {
                    let iterable =
                        self.expression(field(*clause, "right").expect("iterable"), &mut inner);
                    let item = self.fresh("item");
                    let left = field(*clause, "left").expect("target");
                    let mut names = BTreeSet::new();
                    collect_identifiers(left, self.src, &mut names);
                    let mut declares = String::new();
                    for name in names {
                        inner.locals.insert(name.clone());
                        declares.push_str(&format!("let mut {} = Object::Unbound; ", ident(&name)));
                    }
                    let bind = self.bind_target(left, &item, &mut inner);
                    opening.push_str(&format!(
                        "for {item} in object::iter(&{iterable}) {{ {declares}{bind} "
                    ));
                    closing.push_str("} ");
                }
                "if_clause" => {
                    let condition = self.expression(named(*clause)[0], &mut inner);
                    opening.push_str(&format!(
                        "if !object::truthy(&{condition}) {{ continue; }} "
                    ));
                }
                _ => return self.pending(&scope.function, *clause, "comprehension clause"),
            }
        }
        let (push, build) = if node.kind() == "dictionary_comprehension" {
            let key = self.expression(field(element, "key").expect("key"), &mut inner);
            let value = self.expression(field(element, "value").expect("value"), &mut inner);
            (
                format!("__out.push(({key}, {value}));"),
                "object::dict(__out)",
            )
        } else {
            let value = self.expression(element, &mut inner);
            let build = if node.kind() == "set_comprehension" {
                "object::set(__out)"
            } else {
                "object::list(__out)"
            };
            (format!("__out.push({value});"), build)
        };
        format!("{{ let mut __out = Vec::new(); {opening}{push} {closing}{build} }}")
    }

    fn string(&mut self, node: Node<'_>, scope: &mut Scope) -> String {
        let pieces = if node.kind() == "concatenated_string" {
            named(node)
        } else {
            vec![node]
        };
        let mut parts: Vec<String> = Vec::new();
        let mut literal = String::new();
        let mut is_bytes = false;
        let mut is_format = false;
        let mut bytes: Vec<u8> = Vec::new();
        for piece in pieces {
            let start = children(piece)
                .first()
                .map(|c| self.src(*c).to_owned())
                .unwrap_or_default();
            let prefix = start.trim_end_matches(['"', '\'']).to_ascii_lowercase();
            let raw = prefix.contains('r');
            is_bytes |= prefix.contains('b');
            for child in children(piece) {
                match child.kind() {
                    "string_content" => {
                        // Escape sequences may be children of the content.
                        let mut at = child.start_byte();
                        let mut segments: Vec<(&str, bool)> = Vec::new();
                        for escape in children(child)
                            .into_iter()
                            .filter(|e| e.kind() == "escape_sequence")
                        {
                            segments.push((&self.src[at..escape.start_byte()], false));
                            segments.push((self.src(escape), true));
                            at = escape.end_byte();
                        }
                        segments.push((&self.src[at..child.end_byte()], false));
                        for (segment, escaped) in segments {
                            if !escaped || raw {
                                literal.push_str(segment);
                                bytes.extend(segment.as_bytes());
                                continue;
                            }
                            match decode_escape(segment) {
                                Some(Escaped::Char(c)) => {
                                    literal.push(c);
                                    let mut buf = [0u8; 4];
                                    bytes.extend(c.encode_utf8(&mut buf).as_bytes());
                                }
                                Some(Escaped::Byte(b)) => {
                                    literal.push(char::from(b));
                                    bytes.push(b);
                                }
                                Some(Escaped::Nothing) => {}
                                None => {
                                    return self.pending(&scope.function, child, "escape sequence");
                                }
                            }
                        }
                    }
                    "escape_sequence" => {
                        let escape = self.src(child);
                        if raw {
                            literal.push_str(escape);
                            bytes.extend(escape.as_bytes());
                        } else {
                            match decode_escape(escape) {
                                Some(Escaped::Char(c)) => {
                                    literal.push(c);
                                    let mut buf = [0u8; 4];
                                    bytes.extend(c.encode_utf8(&mut buf).as_bytes());
                                }
                                Some(Escaped::Byte(b)) => {
                                    literal.push(char::from(b));
                                    bytes.push(b);
                                }
                                Some(Escaped::Nothing) => {}
                                None => {
                                    return self.pending(&scope.function, child, "escape sequence");
                                }
                            }
                        }
                    }
                    "interpolation" => {
                        is_format = true;
                        if !literal.is_empty() {
                            parts.push(fill(
                                &self.maps.literals["str"],
                                &[("0", &std::mem::take(&mut literal))],
                            ));
                        }
                        let mut inner = named(child).into_iter();
                        let Some(expression) = inner.next() else {
                            continue;
                        };
                        let value = self.expression(expression, scope);
                        let mut conversion = String::new();
                        let mut spec = String::new();
                        for rest in inner {
                            match rest.kind() {
                                "type_conversion" => {
                                    conversion = self.src(rest).trim_start_matches('!').to_owned()
                                }
                                "format_specifier" => {
                                    spec = self.src(rest).trim_start_matches(':').to_owned()
                                }
                                _ => {}
                            }
                        }
                        parts.push(fill(
                            &self.maps.literals["fstring part"],
                            &[("0", &value), ("1", &conversion), ("2", &spec)],
                        ));
                    }
                    _ => {}
                }
            }
        }
        if is_bytes {
            let items: Vec<String> = bytes.iter().map(|b| format!("{b}u8")).collect();
            return fill(&self.maps.literals["bytes"], &[("0", &items.join(", "))]);
        }
        if !is_format {
            return fill(&self.maps.literals["str"], &[("0", &literal)]);
        }
        if !literal.is_empty() {
            parts.push(fill(&self.maps.literals["str"], &[("0", &literal)]));
        }
        fill(&self.maps.literals["fstring"], &[("0", &parts.join(", "))])
    }
}

enum Escaped {
    Char(char),
    Byte(u8),
    Nothing,
}

fn decode_escape(escape: &str) -> Option<Escaped> {
    let body = escape.strip_prefix('\\')?;
    let simple = match body {
        "n" => Some('\n'),
        "t" => Some('\t'),
        "r" => Some('\r'),
        "\\" => Some('\\'),
        "'" => Some('\''),
        "\"" => Some('"'),
        "0" => Some('\0'),
        "a" => Some('\x07'),
        "b" => Some('\x08'),
        "f" => Some('\x0c'),
        "v" => Some('\x0b'),
        _ => None,
    };
    if let Some(c) = simple {
        return Some(Escaped::Char(c));
    }
    if body.starts_with('\n') || body.starts_with("\r\n") {
        return Some(Escaped::Nothing);
    }
    if let Some(hex) = body.strip_prefix('x') {
        return u8::from_str_radix(hex, 16).ok().map(Escaped::Byte);
    }
    if let Some(hex) = body.strip_prefix('u').or_else(|| body.strip_prefix('U')) {
        return u32::from_str_radix(hex, 16)
            .ok()
            .and_then(char::from_u32)
            .map(Escaped::Char);
    }
    if body.chars().all(|c| c.is_digit(8)) && !body.is_empty() {
        return u8::from_str_radix(body, 8).ok().map(Escaped::Byte);
    }
    None
}

fn parse_int(text: &str) -> Option<i64> {
    let digits = text.replace('_', "").to_ascii_lowercase();
    if let Some(hex) = digits.strip_prefix("0x") {
        return i64::from_str_radix(hex, 16).ok();
    }
    if let Some(oct) = digits.strip_prefix("0o") {
        return i64::from_str_radix(oct, 8).ok();
    }
    if let Some(bin) = digits.strip_prefix("0b") {
        return i64::from_str_radix(bin, 2).ok();
    }
    digits.parse().ok()
}

fn unwrap_decorated(node: Node<'_>) -> (Vec<Node<'_>>, Node<'_>) {
    if node.kind() != "decorated_definition" {
        return (Vec::new(), node);
    }
    let parts = named(node);
    let decorators = parts
        .iter()
        .copied()
        .filter(|n| n.kind() == "decorator")
        .collect();
    let definition = field(node, "definition").expect("definition");
    (decorators, definition)
}

/// Names declared `nonlocal` in `body` itself, or (`nested`) in functions
/// defined anywhere inside it.
fn nonlocal_names(body: Node<'_>, src: &str, nested: bool) -> BTreeSet<String> {
    fn walk(node: Node<'_>, src: &str, depth: usize, nested: bool, out: &mut BTreeSet<String>) {
        if node.kind() == "nonlocal_statement" && (depth > 0) == nested {
            for name in named(node) {
                out.insert(text(name, src).to_owned());
            }
        }
        let deeper = depth + usize::from(matches!(node.kind(), "function_definition" | "lambda"));
        if !nested && deeper > 0 {
            return;
        }
        for child in named(node) {
            walk(child, src, deeper, nested, out);
        }
    }
    let mut out = BTreeSet::new();
    walk(body, src, 0, nested, &mut out);
    out
}

fn contains_kind(node: Node<'_>, kind: &str) -> bool {
    if node.kind() == kind {
        return true;
    }
    named(node).into_iter().any(|child| {
        !matches!(
            child.kind(),
            "function_definition" | "lambda" | "class_definition" | "decorated_definition"
        ) && contains_kind(child, kind)
    })
}

fn collect_identifiers(node: Node<'_>, src: &str, out: &mut BTreeSet<String>) {
    if node.kind() == "identifier" {
        out.insert(text(node, src).to_owned());
    }
    for child in named(node) {
        if child.kind() == "attribute" {
            if let Some(object) = field(child, "object") {
                collect_identifiers(object, src, out);
            }
            continue;
        }
        if child.kind() == "keyword_argument" {
            if let Some(value) = field(child, "value") {
                collect_identifiers(value, src, out);
            }
            continue;
        }
        collect_identifiers(child, src, out);
    }
}

/// Names a function body binds, without descending into nested scopes.
fn collect_locals(body: Node<'_>, src: &str) -> Vec<String> {
    let mut out = BTreeSet::new();
    fn targets(node: Node<'_>, src: &str, out: &mut BTreeSet<String>) {
        match node.kind() {
            "identifier" => {
                out.insert(text(node, src).to_owned());
            }
            "pattern_list"
            | "tuple_pattern"
            | "list_pattern"
            | "tuple"
            | "list"
            | "parenthesized_expression"
            | "expression_list"
            | "list_splat_pattern" => {
                for child in named(node) {
                    targets(child, src, out);
                }
            }
            _ => {}
        }
    }
    fn walk(node: Node<'_>, src: &str, out: &mut BTreeSet<String>) {
        match node.kind() {
            "assignment" | "augmented_assignment" => {
                if let Some(left) = field(node, "left") {
                    targets(left, src, out);
                }
                if let Some(right) = field(node, "right") {
                    walk(right, src, out);
                }
                return;
            }
            "for_statement" => {
                if let Some(left) = field(node, "left") {
                    targets(left, src, out);
                }
            }
            "as_pattern" => {
                if let Some(alias) = named(node).last() {
                    for child in named(*alias) {
                        targets(child, src, out);
                    }
                }
            }
            "function_definition" => {
                if let Some(name) = field(node, "name") {
                    out.insert(text(name, src).to_owned());
                }
                return;
            }
            "class_definition" => {
                if let Some(name) = field(node, "name") {
                    out.insert(text(name, src).to_owned());
                }
                return;
            }
            "lambda"
            | "list_comprehension"
            | "set_comprehension"
            | "dictionary_comprehension"
            | "generator_expression" => return,
            _ => {}
        }
        for child in named(node) {
            walk(child, src, out);
        }
    }
    walk(body, src, &mut out);
    out.into_iter().collect()
}
