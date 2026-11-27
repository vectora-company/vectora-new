extends Area2D

@export var speed: float = 520.0
@export var damage: int = 1

var direction: Vector2 = Vector2.RIGHT


func _ready() -> void:
	body_entered.connect(_on_body_entered)


func _physics_process(delta: float) -> void:
	global_position += direction * speed * delta


func hit(target: Node) -> void:
	if target.has_method("take_damage"):
		target.take_damage(damage)
	queue_free()


func _on_body_entered(body: Node2D) -> void:
	if body.is_in_group("enemies"):
		hit(body)
