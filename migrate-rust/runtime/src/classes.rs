//! Classes defined by translated tests: attributes, methods, inheritance,
//! `property`, `staticmethod` and `classmethod`.

use std::sync::{Arc, Mutex};

use crate::object::{Args, Instance, Native, Object, call, exception_class, func, lock, native};

pub struct ClassObject {
    pub name: String,
    pub bases: Vec<Object>,
    pub attrs: Mutex<Vec<(String, Object)>>,
}

/// Marks how a class attribute binds when read from an instance.
enum Descriptor {
    Property(Object),
    Static(Object),
    ClassMethod(Object),
}

struct DescriptorHandle(Descriptor);

impl Native for DescriptorHandle {
    fn class(&self) -> String {
        match self.0 {
            Descriptor::Property(_) => "property",
            Descriptor::Static(_) => "staticmethod",
            Descriptor::ClassMethod(_) => "classmethod",
        }
        .into()
    }
    fn descriptor(&self) -> Option<(&'static str, Object)> {
        Some(match &self.0 {
            Descriptor::Property(f) => ("property", f.clone()),
            Descriptor::Static(f) => ("static", f.clone()),
            Descriptor::ClassMethod(f) => ("class", f.clone()),
        })
    }
}

pub fn property(function: Object) -> Object {
    native(DescriptorHandle(Descriptor::Property(function)))
}

pub fn staticmethod(function: Object) -> Object {
    native(DescriptorHandle(Descriptor::Static(function)))
}

pub fn classmethod(function: Object) -> Object {
    native(DescriptorHandle(Descriptor::ClassMethod(function)))
}

/// `class Name(bases): attrs`. A class deriving from an exception class is an
/// exception class; bases that are not translated classes are ignored.
pub fn class(name: &str, bases: Vec<Object>, attrs: Vec<(&str, Object)>) -> Object {
    let exception_bases: Vec<String> = bases
        .iter()
        .flat_map(|b| match b {
            Object::Class(c) => {
                let mut chain = vec![c.name.clone()];
                collect_bases(c, &mut chain);
                chain
            }
            _ => Vec::new(),
        })
        .collect();
    if !exception_bases.is_empty() {
        let refs: Vec<&str> = exception_bases.iter().map(String::as_str).collect();
        return Object::Class(exception_class(name, &refs));
    }
    let class = Arc::new(ClassObject {
        name: name.to_owned(),
        bases: bases
            .into_iter()
            .filter(|b| as_class(b).is_some() || matches!(b, Object::Name(_)))
            .collect(),
        attrs: Mutex::new(attrs.into_iter().map(|(k, v)| (k.to_owned(), v)).collect()),
    });
    Object::Native(Arc::new(ClassHandle(class)))
}

fn collect_bases(class: &crate::object::Class, out: &mut Vec<String>) {
    for base in &class.bases {
        out.push(base.name.clone());
        collect_bases(base, out);
    }
}

struct ClassHandle(Arc<ClassObject>);

pub fn as_class(value: &Object) -> Option<Arc<ClassObject>> {
    match value {
        Object::Native(n) => n.class_object(),
        _ => None,
    }
}

impl ClassObject {
    /// Attribute through the class and its bases, in order.
    pub fn lookup(&self, attribute: &str) -> Option<Object> {
        if let Some((_, v)) = lock(&self.attrs).iter().find(|(k, _)| k == attribute) {
            return Some(v.clone());
        }
        self.bases
            .iter()
            .filter_map(as_class)
            .find_map(|b| b.lookup(attribute))
    }

    pub fn is_subclass_of(&self, name: &str) -> bool {
        self.name == name
            || self
                .bases
                .iter()
                .filter_map(as_class)
                .any(|b| b.is_subclass_of(name))
    }
}

/// Bind a class attribute read from `receiver`.
pub fn bind(value: Object, receiver: &Object, class: &Object) -> Object {
    if let Object::Native(n) = &value
        && let Some((kind, function)) = n.descriptor()
    {
        return match kind {
            "property" => call(&function, Args::new(vec![receiver.clone()], vec![])),
            "static" => function,
            _ => bound(function, class.clone()),
        };
    }
    match value {
        Object::Func(..) => bound(value, receiver.clone()),
        other => other,
    }
}

fn bound(function: Object, receiver: Object) -> Object {
    func("method", move |mut args: Args| {
        args.pos.insert(0, receiver.clone());
        call(&function, args)
    })
}

impl Native for ClassHandle {
    fn class(&self) -> String {
        "type".into()
    }

    fn class_object(&self) -> Option<Arc<ClassObject>> {
        Some(self.0.clone())
    }

    fn getattr(&self, this: &Object, attribute: &str) -> Option<Object> {
        if attribute == "__name__" {
            return Some(crate::object::str_(&self.0.name));
        }
        let value = self.0.lookup(attribute)?;
        Some(match &value {
            Object::Native(n) => match n.descriptor() {
                Some(("class", f)) => bound(f, this.clone()),
                Some((_, f)) => f,
                None => value,
            },
            _ => value,
        })
    }

    fn setattr(&self, attribute: &str, value: Object) -> bool {
        let mut attrs = lock(&self.0.attrs);
        attrs.retain(|(k, _)| k != attribute);
        attrs.push((attribute.to_owned(), value));
        true
    }

    fn call(&self, this: &Object, args: Args) -> Option<Object> {
        let instance = Object::Instance(Arc::new(Instance {
            class: self.0.name.clone(),
            attrs: Mutex::new(vec![("__class__".into(), this.clone())]),
        }));
        let init = self.0.lookup("__init__");
        if init.is_none() {
            // A base from the backend builds the instance: the expected red.
            self.0.bases.iter().for_each(crate::object::require_value);
        }
        match init {
            Some(init) => {
                let init = bind(init, &instance, this);
                call(&init, args);
            }
            None => {
                for (key, value) in args.kw {
                    crate::object::setattr(&instance, &key, value);
                }
            }
        }
        Some(instance)
    }
}

/// Attribute of an instance found on its class, bound to the instance.
pub fn instance_attribute(
    instance: &Object,
    attrs: &Mutex<Vec<(String, Object)>>,
    attribute: &str,
) -> Option<Object> {
    let class = lock(attrs)
        .iter()
        .find(|(k, _)| k == "__class__")
        .map(|(_, v)| v.clone())?;
    let value = as_class(&class)?.lookup(attribute)?;
    Some(bind(value, instance, &class))
}

/// `isinstance(value, Class)` for translated classes.
pub fn is_instance(value: &Object, class: &Object) -> Option<bool> {
    let class = as_class(class)?;
    let Object::Instance(i) = value else {
        return Some(false);
    };
    let own = lock(&i.attrs)
        .iter()
        .find(|(k, _)| k == "__class__")
        .map(|(_, v)| v.clone());
    Some(
        own.and_then(|c| as_class(&c))
            .is_some_and(|c| c.is_subclass_of(&class.name)),
    )
}

/// Method resolution order of a translated class, depth first.
fn linearize(class: &Arc<ClassObject>, out: &mut Vec<Object>) {
    for base in &class.bases {
        out.push(base.clone());
        if let Some(inner) = as_class(base) {
            linearize(&inner, out);
        }
    }
}

/// Zero-argument `super()` inside a method of `class`, bound to `receiver`.
pub fn super_(receiver: &Object, class: &str) -> Object {
    let own = match crate::object::getattr_opt(receiver, "__class__") {
        Some(c) if as_class(&c).is_some() => c,
        _ => crate::object::name(class),
    };
    let mut order = vec![own];
    if let Some(c) = as_class(&order[0]) {
        linearize(&c, &mut order);
    }
    let start = order
        .iter()
        .position(|c| as_class(c).is_some_and(|c| c.name == class))
        .map_or(order.len(), |i| i + 1);
    native(SuperProxy {
        receiver: receiver.clone(),
        order: order[start..].to_vec(),
    })
}

struct SuperProxy {
    receiver: Object,
    order: Vec<Object>,
}

impl Native for SuperProxy {
    fn class(&self) -> String {
        "super".into()
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        for base in &self.order {
            match as_class(base) {
                Some(class) => {
                    if let Some((_, value)) =
                        lock(&class.attrs).iter().find(|(k, _)| k == attribute)
                    {
                        return Some(bind(value.clone(), &self.receiver, base));
                    }
                }
                // A backend base owns the attribute: the expected red.
                None => {
                    crate::object::require_value(base);
                    return Some(crate::object::getattr(base, attribute));
                }
            }
        }
        // `object` itself: a no-op constructor and the instance's own attributes.
        let receiver = self.receiver.clone();
        Some(match attribute {
            "__init__" => func("__init__", |_| Object::None),
            "__setattr__" => func("__setattr__", move |args| {
                crate::object::setattr(
                    &receiver,
                    &crate::object::text(&args.bind(0, "name", None)),
                    args.bind(1, "value", None),
                );
                Object::None
            }),
            _ => return crate::object::getattr_opt(&self.receiver, attribute),
        })
    }
}
