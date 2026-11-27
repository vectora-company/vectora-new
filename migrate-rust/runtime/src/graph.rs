//! `networkx.Graph` and `networkx.DiGraph`: nodes and edges with attribute
//! dicts, kept in insertion order.

use std::sync::{Arc, Mutex};

use crate::object::{
    Args, Native, Object, dict, eq, func, items, iter, list, lock, native, raise_new, setitem,
    truthy, tuple,
};

struct Graph {
    directed: bool,
    nodes: Mutex<Vec<(Object, Object)>>,
    edges: Mutex<Vec<(Object, Object, Object)>>,
    attributes: Object,
}

fn attributes(kw: Vec<(String, Object)>) -> Object {
    dict(
        kw.into_iter()
            .map(|(k, v)| (crate::object::str_(&k), v))
            .collect(),
    )
}

fn merge(target: &Object, kw: Vec<(String, Object)>) {
    for (k, v) in kw {
        setitem(target, &crate::object::str_(&k), v);
    }
}

impl Graph {
    fn node(&self, node: &Object) -> Option<Object> {
        lock(&self.nodes)
            .iter()
            .find(|(n, _)| eq(n, node))
            .map(|(_, a)| a.clone())
    }

    fn add_node(&self, node: Object, kw: Vec<(String, Object)>) {
        match self.node(&node) {
            Some(existing) => merge(&existing, kw),
            None => lock(&self.nodes).push((node, attributes(kw))),
        }
    }

    fn same(&self, a: (&Object, &Object), b: (&Object, &Object)) -> bool {
        (eq(a.0, b.0) && eq(a.1, b.1)) || (!self.directed && eq(a.0, b.1) && eq(a.1, b.0))
    }

    fn edge(&self, u: &Object, v: &Object) -> Option<Object> {
        lock(&self.edges)
            .iter()
            .find(|(a, b, _)| self.same((a, b), (u, v)))
            .map(|(_, _, d)| d.clone())
    }

    fn add_edge(&self, u: Object, v: Object, kw: Vec<(String, Object)>) {
        self.add_node(u.clone(), Vec::new());
        self.add_node(v.clone(), Vec::new());
        match self.edge(&u, &v) {
            Some(existing) => merge(&existing, kw),
            None => lock(&self.edges).push((u, v, attributes(kw))),
        }
    }

    fn neighbors(&self, node: &Object, outgoing: bool, incoming: bool) -> Vec<Object> {
        lock(&self.edges)
            .iter()
            .filter_map(|(a, b, _)| {
                if (outgoing || !self.directed) && eq(a, node) {
                    Some(b.clone())
                } else if (incoming || !self.directed) && eq(b, node) {
                    Some(a.clone())
                } else {
                    None
                }
            })
            .collect()
    }

    fn node_list(&self, data: bool) -> Object {
        list(
            lock(&self.nodes)
                .iter()
                .map(|(n, a)| {
                    if data {
                        tuple(vec![n.clone(), a.clone()])
                    } else {
                        n.clone()
                    }
                })
                .collect(),
        )
    }

    fn edge_list(&self, data: bool) -> Object {
        list(
            lock(&self.edges)
                .iter()
                .map(|(a, b, d)| {
                    let mut parts = vec![a.clone(), b.clone()];
                    if data {
                        parts.push(d.clone());
                    }
                    tuple(parts)
                })
                .collect(),
        )
    }
}

struct GraphHandle(Arc<Graph>);

/// `G.nodes` / `G.edges`: iterable, indexable by key and callable with `data=`.
struct View {
    graph: Arc<Graph>,
    edges: bool,
}

impl Native for View {
    fn class(&self) -> String {
        if self.edges { "EdgeView" } else { "NodeView" }.into()
    }
    fn iterate(&self) -> Option<Vec<Object>> {
        let all = if self.edges {
            self.graph.edge_list(false)
        } else {
            self.graph.node_list(false)
        };
        Some(iter(&all))
    }
    fn contains(&self, item: &Object) -> Option<bool> {
        Some(if self.edges {
            let pair = iter(item);
            pair.len() == 2 && self.graph.edge(&pair[0], &pair[1]).is_some()
        } else {
            self.graph.node(item).is_some()
        })
    }
    fn getitem(&self, key: &Object) -> Option<Object> {
        Some(if self.edges {
            let pair = iter(key);
            self.graph
                .edge(&pair[0], &pair[1])
                .unwrap_or_else(|| raise_new("KeyError", &crate::object::text(key)))
        } else {
            self.graph
                .node(key)
                .unwrap_or_else(|| raise_new("KeyError", &crate::object::text(key)))
        })
    }
    fn call(&self, _this: &Object, args: Args) -> Option<Object> {
        let data = args.kwarg("data").is_some_and(|d| truthy(&d));
        Some(if self.edges {
            self.graph.edge_list(data)
        } else {
            self.graph.node_list(data)
        })
    }
}

fn graph(directed: bool, args: Args) -> Object {
    let graph = Arc::new(Graph {
        directed,
        nodes: Mutex::new(Vec::new()),
        edges: Mutex::new(Vec::new()),
        attributes: attributes(args.kw.clone()),
    });
    if let Some(edges) = args
        .arg(0, "incoming_graph_data")
        .filter(|e| !matches!(e, Object::None))
    {
        for edge in iter(&edges) {
            let pair = iter(&edge);
            graph.add_edge(pair[0].clone(), pair[1].clone(), Vec::new());
        }
    }
    native(GraphHandle(graph))
}

pub fn undirected(args: Args) -> Object {
    graph(false, args)
}

pub fn directed(args: Args) -> Object {
    graph(true, args)
}

impl Native for GraphHandle {
    fn class(&self) -> String {
        if self.0.directed { "DiGraph" } else { "Graph" }.into()
    }
    fn iterate(&self) -> Option<Vec<Object>> {
        Some(iter(&self.0.node_list(false)))
    }
    fn contains(&self, item: &Object) -> Option<bool> {
        Some(self.0.node(item).is_some())
    }
    fn getitem(&self, key: &Object) -> Option<Object> {
        let neighbors = self.0.neighbors(key, true, false);
        Some(dict(
            neighbors
                .into_iter()
                .map(|n| {
                    let data = self.0.edge(key, &n).unwrap_or_else(|| dict(Vec::new()));
                    (n, data)
                })
                .collect(),
        ))
    }
    fn getattr(&self, _this: &Object, attribute: &str) -> Option<Object> {
        let graph = self.0.clone();
        Some(match attribute {
            "nodes" => native(View {
                graph,
                edges: false,
            }),
            "edges" => native(View { graph, edges: true }),
            "graph" => self.0.attributes.clone(),
            "add_node" => func("add_node", move |args: Args| {
                graph.add_node(args.bind(0, "node_for_adding", None), args.kw);
                Object::None
            }),
            "add_nodes_from" => func("add_nodes_from", move |args: Args| {
                for node in iter(&args.bind(0, "nodes_for_adding", None)) {
                    match &node {
                        Object::Tuple(t) if t.len() == 2 && matches!(t[1], Object::Dict(_)) => {
                            let kw = items(&t[1])
                                .into_iter()
                                .map(|(k, v)| (crate::object::text(&k), v))
                                .collect();
                            graph.add_node(t[0].clone(), kw);
                        }
                        _ => graph.add_node(node.clone(), args.kw.clone()),
                    }
                }
                Object::None
            }),
            "add_edge" => func("add_edge", move |args: Args| {
                graph.add_edge(
                    args.bind(0, "u_of_edge", None),
                    args.bind(1, "v_of_edge", None),
                    args.kw,
                );
                Object::None
            }),
            "add_edges_from" => func("add_edges_from", move |args: Args| {
                for edge in iter(&args.bind(0, "ebunch_to_add", None)) {
                    let parts = iter(&edge);
                    let mut kw = args.kw.clone();
                    if let Some(data) = parts.get(2) {
                        kw.extend(
                            items(data)
                                .into_iter()
                                .map(|(k, v)| (crate::object::text(&k), v)),
                        );
                    }
                    graph.add_edge(parts[0].clone(), parts[1].clone(), kw);
                }
                Object::None
            }),
            "has_node" => func("has_node", move |args| {
                Object::Bool(graph.node(&args.bind(0, "n", None)).is_some())
            }),
            "has_edge" => func("has_edge", move |args| {
                Object::Bool(
                    graph
                        .edge(&args.bind(0, "u", None), &args.bind(1, "v", None))
                        .is_some(),
                )
            }),
            "number_of_nodes" => func("number_of_nodes", move |_| {
                Object::Int(lock(&graph.nodes).len() as i64)
            }),
            "number_of_edges" => func("number_of_edges", move |_| {
                Object::Int(lock(&graph.edges).len() as i64)
            }),
            "is_directed" => func("is_directed", move |_| Object::Bool(graph.directed)),
            "neighbors" | "successors" => func(attribute, move |args| {
                list(graph.neighbors(&args.bind(0, "n", None), true, false))
            }),
            "predecessors" => func("predecessors", move |args| {
                list(graph.neighbors(&args.bind(0, "n", None), false, true))
            }),
            "degree" => func("degree", move |args| {
                let node = args.bind(0, "nbunch", None);
                Object::Int(graph.neighbors(&node, true, true).len() as i64)
            }),
            "remove_node" => func("remove_node", move |args| {
                let node = args.bind(0, "n", None);
                lock(&graph.nodes).retain(|(n, _)| !eq(n, &node));
                lock(&graph.edges).retain(|(a, b, _)| !eq(a, &node) && !eq(b, &node));
                Object::None
            }),
            _ => return None,
        })
    }
}
