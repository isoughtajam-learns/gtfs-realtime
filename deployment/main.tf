terraform {
  required_version = ">= 1.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  container_port = 8000

  # Every backend service (backend, celery-worker, celery-beat) runs the
  # single image built from ../gtfs-realtime's Dockerfile, at the git tag
  # named by var.backend_image_tag.
  image = "${aws_ecr_repository.app.repository_url}:${var.backend_image_tag}"

  # Provisioned by hand via the Secrets Manager console, not by Terraform -
  # referenced by ecs_secrets_access below (which grants the shared execution
  # role access to them) rather than duplicated, so the two can't silently
  # drift apart. The frontend task definition itself now lives in
  # ../gtfs-dashboard/deployment (its own Terraform stack) and looks these
  # secrets up by name via a data source - but it uses THIS role, so this
  # stack still owns granting it access.
  tls_cert_arn = "arn:aws:secretsmanager:us-east-2:537735702437:secret:gtfs-realtime/tls-cert"
  tls_key_arn  = "arn:aws:secretsmanager:us-east-2:537735702437:secret:gtfs-realtime/tls-key"

  common_environment = [
    { name = "ENV", value = "prod" },
    { name = "APP_NAME", value = var.app_name },
    { name = "ADMIN_EMAIL", value = var.admin_email },
    { name = "DEBUG", value = "false" },
    { name = "CELERY_BROKER_URL", value = "redis://${aws_elasticache_cluster.redis.cache_nodes[0].address}:6379/0" },
    # Python block-buffers stdout when it isn't a TTY (always true in a
    # container) - without this, prints/tracebacks vanish from CloudWatch
    # if the process is killed before the buffer flushes.
    { name = "PYTHONUNBUFFERED", value = "1" },
  ]

  common_secrets = [
    { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
    { name = "SECRET_KEY", valueFrom = aws_secretsmanager_secret.app_secret_key.arn },
  ]

  log_config = { for prefix in ["backend", "celery-worker", "celery-beat"] : prefix => {
    logDriver = "awslogs"
    options = {
      "awslogs-group"         = aws_cloudwatch_log_group.ecs_logs.name
      "awslogs-region"        = var.aws_region
      "awslogs-stream-prefix" = prefix
    }
  } }
}

# ------------------------------------------------------------------------------
# 1. Networking (Using Default VPC & Subnets for Simplicity)
# ------------------------------------------------------------------------------
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# The single EC2 container instance: app port + SSH inbound.
resource "aws_security_group" "ec2" {
  name        = "${var.app_name}-ec2-sg"
  description = "ECS EC2 container instance - app + SSH"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "Backend API inbound"
    from_port   = local.container_port
    to_port     = local.container_port
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Dashboard (frontend) inbound"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Dashboard (frontend) HTTPS inbound - Cloudflare origin connection"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ssh_ingress_cidr]
  }

  egress {
    description = "Allow all outbound traffic (ECR pulls, RDS, ElastiCache, GTFS feeds)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# RDS: only reachable from the ECS container instance.
resource "aws_security_group" "rds" {
  name        = "${var.app_name}-rds-sg"
  description = "Allow Postgres access from ECS tasks"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description     = "Postgres from the ECS container instance"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ec2.id]
  }
}

# ElastiCache: only reachable from the ECS container instance.
resource "aws_security_group" "redis" {
  name        = "${var.app_name}-redis-sg"
  description = "Allow Redis access from ECS tasks"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description     = "Redis from the ECS container instance"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.ec2.id]
  }
}

# ------------------------------------------------------------------------------
# 2. Amazon ECR - single repository for the one image the Dockerfile builds
# ------------------------------------------------------------------------------
resource "aws_ecr_repository" "app" {
  name                 = var.app_name
  image_tag_mutability = "MUTABLE"
}

# ------------------------------------------------------------------------------
# 3. Data stores (Postgres/Redis stay managed - Fargate/EC2 container storage
#    is still ephemeral, so these remain RDS + ElastiCache)
# ------------------------------------------------------------------------------
resource "random_password" "db_password" {
  length  = 24
  special = false
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.app_name}-db-subnet-group"
  subnet_ids = data.aws_subnets.default.ids
}

resource "aws_db_instance" "postgres" {
  identifier              = "${var.app_name}-db"
  engine                  = "postgres"
  engine_version          = "15"
  instance_class          = "db.t3.micro"
  allocated_storage       = 20
  storage_encrypted       = true
  db_name                 = var.db_name
  username                = var.db_username
  password                = random_password.db_password.result
  db_subnet_group_name    = aws_db_subnet_group.main.name
  vpc_security_group_ids  = [aws_security_group.rds.id]
  publicly_accessible     = false
  multi_az                = false
  backup_retention_period = 1
  skip_final_snapshot     = true
}

resource "aws_elasticache_subnet_group" "main" {
  name       = "${var.app_name}-redis-subnet-group"
  subnet_ids = data.aws_subnets.default.ids
}

resource "aws_elasticache_cluster" "redis" {
  cluster_id         = "${var.app_name}-redis"
  engine             = "redis"
  engine_version     = "7.1"
  node_type          = "cache.t3.micro"
  num_cache_nodes    = 1
  port               = 6379
  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.redis.id]
}

# ------------------------------------------------------------------------------
# 4. Secrets - DATABASE_URL embeds the RDS password, so it's injected via
#    Secrets Manager rather than a plaintext task-definition environment var.
# ------------------------------------------------------------------------------
resource "random_password" "app_secret_key" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "database_url" {
  name = "${var.app_name}/database-url"
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = "postgresql+psycopg2://${var.db_username}:${random_password.db_password.result}@${aws_db_instance.postgres.address}:5432/${var.db_name}"
}

resource "aws_secretsmanager_secret" "app_secret_key" {
  name = "${var.app_name}/app-secret-key"
}

resource "aws_secretsmanager_secret_version" "app_secret_key" {
  secret_id     = aws_secretsmanager_secret.app_secret_key.id
  secret_string = random_password.app_secret_key.result
}

# ------------------------------------------------------------------------------
# 5. CloudWatch Logging
# ------------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "ecs_logs" {
  name              = "/ecs/${var.app_name}"
  retention_in_days = 7
}

# ------------------------------------------------------------------------------
# 6. IAM Role for ECS Task Execution (pulls image, resolves secrets, ships logs)
# ------------------------------------------------------------------------------
resource "aws_iam_role" "ecs_execution_role" {
  name = "${var.app_name}-execution-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_execution_attach" {
  role       = aws_iam_role.ecs_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_secrets_access" {
  name = "${var.app_name}-secrets-access"
  role = aws_iam_role.ecs_execution_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          aws_secretsmanager_secret.database_url.arn,
          aws_secretsmanager_secret.app_secret_key.arn,
          local.tls_cert_arn,
          local.tls_key_arn,
        ]
      }
    ]
  })
}

# ------------------------------------------------------------------------------
# 7. IAM Role for the EC2 container instance itself (lets the ECS agent
#    register with the cluster, pull images, and ship logs on the host's behalf)
# ------------------------------------------------------------------------------
resource "aws_iam_role" "ecs_instance_role" {
  name = "${var.app_name}-ecs-instance-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_instance_role" {
  role       = aws_iam_role.ecs_instance_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_instance_profile" "ecs_instance" {
  name = "${var.app_name}-ecs-instance-profile"
  role = aws_iam_role.ecs_instance_role.name
}

# ------------------------------------------------------------------------------
# 8. ECS Cluster
# ------------------------------------------------------------------------------
resource "aws_ecs_cluster" "main" {
  name = "${var.app_name}-cluster"
}

# ------------------------------------------------------------------------------
# 9. EC2 Container Instance + Elastic IP
# ------------------------------------------------------------------------------
data "aws_ssm_parameter" "ecs_ami" {
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2/arm64/recommended/image_id"
}

resource "aws_instance" "ecs" {
  ami                         = data.aws_ssm_parameter.ecs_ami.value
  instance_type               = var.instance_type
  subnet_id                   = data.aws_subnets.default.ids[0]
  associate_public_ip_address = true
  iam_instance_profile        = aws_iam_instance_profile.ecs_instance.name
  vpc_security_group_ids      = [aws_security_group.ec2.id]
  key_name                    = var.ssh_key_name

  # Registers the instance with the ECS cluster on boot.
  user_data = base64encode(<<-EOF
    #!/bin/bash
    echo ECS_CLUSTER=${aws_ecs_cluster.main.name} >> /etc/ecs/ecs.config
  EOF
  )

  tags = {
    Name = "${var.app_name}-ecs-instance"
  }
}

# Static public IP - survives instance stop/start/replace.
resource "aws_eip" "ecs" {
  domain   = "vpc"
  instance = aws_instance.ecs.id

  tags = {
    Name = "${var.app_name}-eip"
  }
}

# ------------------------------------------------------------------------------
# 10. Task Definitions - backend/celery-worker/celery-beat run the single
#     image from this repo's Dockerfile, EC2 launch type + host networking.
#     The frontend task definition/service now live in
#     ../gtfs-dashboard/deployment (its own Terraform stack) - it shares this
#     stack's EC2 instance/ECS cluster/execution role (looked up by name, not
#     a state reference) and reaches the backend via localhost since it's on
#     the same host.
# ------------------------------------------------------------------------------
resource "aws_ecs_task_definition" "backend" {
  family                   = "${var.app_name}-backend"
  network_mode             = "host"
  requires_compatibilities = ["EC2"]
  execution_role_arn       = aws_iam_role.ecs_execution_role.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name              = "backend"
      image             = local.image
      essential         = true
      cpu               = 256
      memoryReservation = 256
      command = [
        "sh", "-c",
        "alembic upgrade head && uvicorn src.main:app --host 0.0.0.0 --port 8000"
      ]
      portMappings = [
        {
          containerPort = local.container_port
          hostPort      = local.container_port
          protocol      = "tcp"
        }
      ]
      environment      = local.common_environment
      secrets          = local.common_secrets
      logConfiguration = local.log_config["backend"]
    }
  ])
}

resource "aws_ecs_task_definition" "celery_worker" {
  family                   = "${var.app_name}-celery-worker"
  network_mode             = "host"
  requires_compatibilities = ["EC2"]
  execution_role_arn       = aws_iam_role.ecs_execution_role.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name              = "celery-worker"
      image             = local.image
      essential         = true
      cpu               = 256
      memoryReservation = 256
      command           = ["celery", "-A", "src.tasks", "worker", "--loglevel=info"]
      environment       = local.common_environment
      secrets           = local.common_secrets
      logConfiguration  = local.log_config["celery-worker"]
    }
  ])
}

resource "aws_ecs_task_definition" "celery_beat" {
  family                   = "${var.app_name}-celery-beat"
  network_mode             = "host"
  requires_compatibilities = ["EC2"]
  execution_role_arn       = aws_iam_role.ecs_execution_role.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name              = "celery-beat"
      image             = local.image
      essential         = true
      cpu               = 128
      memoryReservation = 128
      command           = ["celery", "-A", "src.tasks", "beat", "--loglevel=info"]
      environment       = local.common_environment
      secrets           = local.common_secrets
      logConfiguration  = local.log_config["celery-beat"]
    }
  ])
}

# ------------------------------------------------------------------------------
# 11. ECS Services - EC2 launch type, no network_configuration/load balancer;
#     tasks are placed directly on the one container instance.
# ------------------------------------------------------------------------------
resource "aws_ecs_service" "backend" {
  name            = "${var.app_name}-backend"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.backend.arn
  desired_count   = var.backend_desired_count
  launch_type     = "EC2"

  force_new_deployment = true
  # Single instance + fixed host port 8000 (bridge mode): the new task can't
  # start alongside the old one without a port conflict, so deploys must stop
  # the old task first rather than the default start-new-then-stop-old.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
}

resource "aws_ecs_service" "celery_worker" {
  name            = "${var.app_name}-celery-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.celery_worker.arn
  desired_count   = var.celery_worker_desired_count
  launch_type     = "EC2"

  force_new_deployment               = true
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
}

resource "aws_ecs_service" "celery_beat" {
  name            = "${var.app_name}-celery-beat"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.celery_beat.arn
  # Must stay at 1 - celery beat schedules tasks, running more than one
  # instance would enqueue duplicate jobs.
  desired_count = 1
  launch_type   = "EC2"

  force_new_deployment               = true
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
}
