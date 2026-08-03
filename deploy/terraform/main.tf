# Clinical Co-Pilot — 3-tier fleet on AWS (DB / EMR / Agent), all t3.micro.
# Provisions the shape once; day-to-day power is handled by deploy/scripts/fleet.py.
# Boxes talk over private IPs (stable across stop/start); only the agent box is public.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

locals {
  project = "clinical-copilot"
}

# --- lookups: default VPC + one subnet + latest Ubuntu 24.04 AMI ---

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd*/ubuntu-noble-24.04-amd64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

locals {
  subnet_id = tolist(data.aws_subnets.default.ids)[0]
}

# --- security groups (tiered; each opens only to the tier above it) ---

resource "aws_security_group" "agent" {
  name_prefix = "${local.project}-agent-"
  description = "Agent box: public web (80/443) + SSH"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "HTTP (Caddy ACME + redirect)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "HTTPS (public demo)"
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
    cidr_blocks = [var.my_ip_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${local.project}-agent", Project = local.project, Role = "agent" }
}

resource "aws_security_group" "emr" {
  name_prefix = "${local.project}-emr-"
  description = "EMR box: FHIR/HTTPS from agent + SSH"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description     = "FHIR/HTTPS from the agent box only"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.agent.id]
  }
  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.my_ip_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${local.project}-emr", Project = local.project, Role = "emr" }
}

resource "aws_security_group" "db" {
  name_prefix = "${local.project}-db-"
  description = "DB box: MariaDB 3306 from EMR + SSH"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description     = "MariaDB from the EMR box only"
    from_port       = 3306
    to_port         = 3306
    protocol        = "tcp"
    security_groups = [aws_security_group.emr.id]
  }
  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.my_ip_cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${local.project}-db", Project = local.project, Role = "db" }
}

# --- IAM: let the instances pull from ECR (no keys on the boxes) ---

resource "aws_iam_role" "ec2" {
  name_prefix = "${local.project}-ec2-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Project = local.project }
}

resource "aws_iam_role_policy_attachment" "ecr_read" {
  role       = aws_iam_role.ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_iam_instance_profile" "ec2" {
  name_prefix = "${local.project}-ec2-"
  role        = aws_iam_role.ec2.name
}

# --- instances ---

resource "aws_instance" "db" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = var.instance_type
  key_name                    = var.key_name
  subnet_id                   = local.subnet_id
  vpc_security_group_ids      = [aws_security_group.db.id]
  associate_public_ip_address = true
  iam_instance_profile        = aws_iam_instance_profile.ec2.name
  user_data                   = file("${path.module}/cloud-init.sh")

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }
  tags = { Name = "${local.project}-db", Project = local.project, Role = "db" }
}

resource "aws_instance" "emr" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = var.instance_type
  key_name                    = var.key_name
  subnet_id                   = local.subnet_id
  vpc_security_group_ids      = [aws_security_group.emr.id]
  associate_public_ip_address = true
  iam_instance_profile        = aws_iam_instance_profile.ec2.name
  user_data                   = file("${path.module}/cloud-init.sh")

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }
  tags = { Name = "${local.project}-emr", Project = local.project, Role = "emr" }
}

resource "aws_instance" "agent" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = var.instance_type
  key_name                    = var.key_name
  subnet_id                   = local.subnet_id
  vpc_security_group_ids      = [aws_security_group.agent.id]
  associate_public_ip_address = true
  iam_instance_profile        = aws_iam_instance_profile.ec2.name
  user_data                   = file("${path.module}/cloud-init.sh")

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }
  tags = { Name = "${local.project}-agent", Project = local.project, Role = "agent" }
}

# Stable public IP for the agent box so DNS never chases a stop/start.
resource "aws_eip" "agent" {
  instance = aws_instance.agent.id
  domain   = "vpc"
  tags     = { Name = "${local.project}-agent", Project = local.project }
}
