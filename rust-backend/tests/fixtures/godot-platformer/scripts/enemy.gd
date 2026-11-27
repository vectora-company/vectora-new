extends CharacterBody2D

signal defeated

const GRAVITY: float = 980.0

@export var move_speed: float = 70.0
@export var max_health: int = 2
@export var contact_damage: int = 1

var health: int = 2
var target: Node2D
var attack_cooldown: float = 0.0


func _ready() -> void:
	health = max_health


func _physics_process(delta: float) -> void:
	attack_cooldown = maxf(attack_cooldown - delta, 0.0)
	if not is_on_floor():
		velocity.y += GRAVITY * delta
	if is_instance_valid(target) and GameManager.status == "playing":
		velocity.x = signf(target.global_position.x - global_position.x) * move_speed
	else:
		velocity.x = 0.0
	move_and_slide()
	for index in get_slide_collision_count():
		var collider: Object = get_slide_collision(index).get_collider()
		if collider == target:
			attack_player(target)


func attack_player(player: Node) -> void:
	if attack_cooldown > 0.0 or GameManager.status != "playing":
		return
	attack_cooldown = 0.7
	player.take_damage(contact_damage)


func take_damage(amount: int = 1) -> void:
	if amount <= 0 or health <= 0:
		return
	health = maxi(health - amount, 0)
	if health == 0:
		GameManager.enemy_defeated()
		defeated.emit()
		queue_free()
