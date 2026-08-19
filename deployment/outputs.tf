output "ecr_repository_url" {
  description = "URL of the backend ECR repository - push the image built from ../gtfs-realtime's Dockerfile here"
  value       = aws_ecr_repository.app.repository_url
}

output "ecs_cluster_name" {
  description = "Name of the deployed ECS Cluster"
  value       = aws_ecs_cluster.main.name
}

output "backend_service_name" {
  description = "Name of the backend ECS service"
  value       = aws_ecs_service.backend.name
}

output "celery_worker_service_name" {
  description = "Name of the celery worker ECS service"
  value       = aws_ecs_service.celery_worker.name
}

output "celery_beat_service_name" {
  description = "Name of the celery beat ECS service"
  value       = aws_ecs_service.celery_beat.name
}

output "aws_region" {
  description = "AWS region resources were deployed into"
  value       = var.aws_region
}

output "rds_endpoint" {
  description = "Postgres endpoint (host) used to build DATABASE_URL"
  value       = aws_db_instance.postgres.address
}

output "redis_endpoint" {
  description = "Redis endpoint (host) used to build CELERY_BROKER_URL"
  value       = aws_elasticache_cluster.redis.cache_nodes[0].address
}

output "database_url_secret_arn" {
  description = "Secrets Manager ARN holding the full DATABASE_URL"
  value       = aws_secretsmanager_secret.database_url.arn
}

output "app_secret_key_secret_arn" {
  description = "Secrets Manager ARN holding the app's SECRET_KEY"
  value       = aws_secretsmanager_secret.app_secret_key.arn
}

output "static_public_ip" {
  description = "Static public IP (Elastic IP) of the ECS container instance - survives stop/start/replace"
  value       = aws_eip.ecs.public_ip
}

output "app_url" {
  description = "Dashboard URL (frontend, proxies /api/ to the backend)"
  value       = "http://${aws_eip.ecs.public_ip}"
}

output "backend_api_url" {
  description = "Direct URL to the backend API (bypassing the frontend's /api/ proxy)"
  value       = "http://${aws_eip.ecs.public_ip}:8000"
}

output "ssh_command" {
  description = "SSH command to reach the container instance for debugging"
  value       = "ssh -i ~/.ssh/${var.ssh_key_name}.pem ec2-user@${aws_eip.ecs.public_ip}"
}
