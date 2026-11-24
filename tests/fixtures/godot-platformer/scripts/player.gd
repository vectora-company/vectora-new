extends CharacterBody2D

signal damaged(health: int)

const BULLET_SCENE: PackedScene = preload("res://scenes/bullet.tscn")
const GRAVITY: float = 980.0

@export var move_speed: float = 220.0
@export var jump_speed: float = 440.0
@export var max_health: int = 3

var health: int = 3


func _ready() -> void:
	health = max_health


func _physics_process(delta: float) -> void:
	if not is_on_floor():
		velocity.y += GRAVITY * delta
	move_and_slide()


func apply_movement(horizontal: float, jump_requested: bool = false) -> void:
	velocity.x = clampf(horizontal, -1.0, 1.0) * move_speed
	if jump_requested and is_on_floor():
		velocity.y = -jump_speed


func take_damage(amount: int = 1) -> void:
	if amount <= 0 or health <= 0:
		return
	health = maxi(health - amount, 0)
	GameManager.player_damaged(amount)
	damaged.emit(health)


func shoot(direction: Vector2 = Vector2.RIGHT) -> Area2D:
	var bullet: Area2D = BULLET_SCENE.instantiate()
	bullet.global_position = global_position + Vector2(18.0, -8.0)
	bullet.set("direction", direction.normalized())
	get_tree().current_scene.add_child(bullet)
	return bullet
