variable "region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "key_name" {
  description = "Name of an existing EC2 key pair (for SSH). Create it in the console first."
  type        = string
}

variable "my_ip_cidr" {
  description = "Your public IP as a /32 CIDR for SSH access, e.g. 203.0.113.4/32."
  type        = string
}

variable "instance_type" {
  description = "Instance type for all three tiers."
  type        = string
  default     = "t3.micro"
}

variable "domain" {
  description = "Public subdomain for the agent box (informational; used in the runbook/DNS)."
  type        = string
  default     = "copilot.aspenitservices.com"
}
