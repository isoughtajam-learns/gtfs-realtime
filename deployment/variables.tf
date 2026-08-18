variable "aws_region" {
  type        = string
  default     = "us-east-2"
  description = "AWS Region to deploy resources"
}

variable "app_name" {
  type        = string
  default     = "gtfs-realtime"
  description = "Name prefix for application resources"
}

variable "backend_image_tag" {
  type        = string
  description = "Git tag (e.g. v0.2.2) of this repo to deploy. Must be a tag reachable from origin/main - deploy.sh enforces this and pushes the matching image before applying."
}

variable "admin_email" {
  type        = string
  default     = "admin@example.com"
  description = "Value for the app's ADMIN_EMAIL setting"
}

variable "db_name" {
  type        = string
  default     = "gtfs"
  description = "Postgres database name"
}

variable "db_username" {
  type        = string
  default     = "postgres"
  description = "Postgres master username"
}

variable "backend_desired_count" {
  type        = number
  default     = 1
  description = "Number of backend (FastAPI/uvicorn) tasks to run"
}

variable "celery_worker_desired_count" {
  type        = number
  default     = 1
  description = "Number of celery worker tasks to run"
}

variable "instance_type" {
  type        = string
  default     = "t4g.small"
  description = "EC2 instance type for the single ECS container instance. Steady-state container memoryReservation totals ~768MB (backend/celery-worker/celery-beat/frontend at 256/256/128/128 MB - frontend's own resources now live in ../gtfs-dashboard/deployment, but it still runs on this instance), but the backend's startup fetch (parsing all 4 GTFS Schedule datasets into memory) spikes well above that - t4g.micro's 1GB got the box unresponsive under that spike. Note: this AWS account's \"Free Plan\" rejects t4g.medium and larger with FreeTierRestrictionError - t4g.small is the largest allowed."
}

variable "ssh_key_name" {
  type        = string
  default     = "main laptop"
  description = "Name of an existing EC2 key pair to allow SSH access to the instance. Create one first: aws ec2 create-key-pair --key-name <name> --query 'KeyMaterial' --output text > ~/.ssh/<name>.pem && chmod 400 ~/.ssh/<name>.pem"
}

variable "ssh_ingress_cidr" {
  type        = string
  default     = "0.0.0.0/0"
  description = "CIDR allowed to SSH into the instance on port 22. Restrict to your IP (e.g. \"1.2.3.4/32\") instead of leaving this open to the internet."
}
