# Private IPs are what the tiers use to reach each other (stable across stop/start).
# Public IPs are for your SSH; the agent's is an Elastic IP, so point DNS at it.

output "db_private_ip" {
  description = "MariaDB box private IP — set as MYSQL_HOST on the EMR box."
  value       = aws_instance.db.private_ip
}

output "emr_private_ip" {
  description = "OpenEMR box private IP — the Co-Pilot's OPENEMR_BASE host on the agent box."
  value       = aws_instance.emr.private_ip
}

output "agent_public_ip" {
  description = "Agent box Elastic IP — create the DNS A record pointing here."
  value       = aws_eip.agent.public_ip
}

output "ssh" {
  description = "SSH commands for each box (swap in your key path)."
  value = {
    db    = "ssh -i <key>.pem ubuntu@${aws_instance.db.public_ip}"
    emr   = "ssh -i <key>.pem ubuntu@${aws_instance.emr.public_ip}"
    agent = "ssh -i <key>.pem ubuntu@${aws_eip.agent.public_ip}"
  }
}

output "dns_record" {
  description = "The A record to create for the public demo."
  value       = "${var.domain}  A  ${aws_eip.agent.public_ip}"
}
