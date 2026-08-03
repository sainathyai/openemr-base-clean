# Deploying the Clinical Co-Pilot (3-tier, baked images, on AWS)

Production-shaped fleet: **DB → EMR → Agent**, three t3.micro boxes, each running one
pre-built image pulled from ECR. Terraform owns the fleet's shape; `scripts/fleet.py`
owns its power (start/stop together). Only the agent tier is redeployed as you iterate.

```
Box 1 (t3.micro)      Box 2 (t3.micro)        Box 3 (t3.micro)
  copilot-db     <--    copilot-emr      <--    copilot-agent + Caddy
  (MariaDB,             (OpenEMR 8.2-dev,        (FastAPI, live FHIR)
   seeded)              baked htdocs)            + public TLS
   :3306 from EMR SG     :443 from Agent SG       :80/:443 public
```

All three images are already built and validated together locally (seeded DB, baked
OpenEMR, live agent). This runbook pushes them to ECR and stands up the fleet.

---

## 0. Prerequisites

- Images built locally: `copilot-db:local`, `copilot-emr:local`, `clinical-copilot:latest`.
- An EC2 **key pair** created in the console (download the `.pem`).
- A **domain** you can add an A record to (e.g. `copilot.aspenitservices.com`).
- Terraform and the AWS CLI installed on your machine.

## 1. IAM user (your machine) — *for Terraform + ECR*

Console → IAM → Users → Create user (e.g. `copilot-deployer`), **programmatic access**.
Attach these managed policies (a dedicated user; delete it when the project is done):

- `AmazonEC2FullAccess`
- `AmazonEC2ContainerRegistryFullAccess`
- `IAMFullAccess`  (Terraform creates the instances' ECR-read role; remove the user after)

Create an access key, then:

```bash
aws configure          # paste key, secret, region (e.g. us-east-1)
aws sts get-caller-identity   # confirm it's the deployer user
```

## 2. Push the images to ECR

```bash
cd deploy/scripts
AWS_REGION=us-east-1 bash push-images.sh
```

Creates the `copilot-db` / `copilot-emr` / `copilot-agent` repos, logs in, pushes.
Note: `copilot-emr` is ~2.9 GB, so this one-time push is bandwidth-bound on your
uplink. The script prints the three image URIs — keep them for the box `.env` files.

## 3. Provision the fleet with Terraform

```bash
cd ../terraform
cp terraform.tfvars.example terraform.tfvars
#   key_name   = your EC2 key pair name
#   my_ip_cidr = your IP /32   (curl -s https://checkip.amazonaws.com)/32
#   region     = same as ECR
#   domain     = your subdomain
terraform init
terraform apply
```

Note the outputs: `db_private_ip`, `emr_private_ip`, `agent_public_ip`, and the
`ssh` commands. Launching the instances also earns the $20 "Launch an instance" credit.

## 4. Point DNS

Add an A record: `your.subdomain  ->  agent_public_ip` (the Elastic IP). Confirm with
`nslookup your.subdomain`. Caddy's TLS in step 5c depends on this.

## 5. Bring up each box

The boxes already have Docker + the AWS CLI (cloud-init) and an ECR-read role, so no
keys are needed on them. On **each** box: SSH in, fetch its compose, set `.env`, log in
to ECR, `up`. Replace `<acct>`/`<region>` and the private IPs with your values.

**5a. DB box**
```bash
ssh -i <key>.pem ubuntu@<db_public_ip>
mkdir -p ~/db && cd ~/db
curl -fsSLO https://raw.githubusercontent.com/sainathyai/openemr-base-clean/feat/clinical-copilot-foundation/deploy/boxes/db/docker-compose.yml
cat > .env <<EOF
DB_IMAGE=<acct>.dkr.ecr.<region>.amazonaws.com/copilot-db:latest
DB_ROOT_PASSWORD=<a-strong-password>
EOF
aws ecr get-login-password --region <region> | docker login --username AWS --password-stdin <acct>.dkr.ecr.<region>.amazonaws.com
docker compose up -d
docker compose logs -f      # wait for the seed load + "ready for connections"
```

**5b. EMR box**  (uses the DB box private IP)
```bash
ssh -i <key>.pem ubuntu@<emr_public_ip>
mkdir -p ~/emr && cd ~/emr
curl -fsSLO https://raw.githubusercontent.com/sainathyai/openemr-base-clean/feat/clinical-copilot-foundation/deploy/boxes/emr/docker-compose.yml
cat > .env <<EOF
EMR_IMAGE=<acct>.dkr.ecr.<region>.amazonaws.com/copilot-emr:latest
DB_HOST=<db_private_ip>
EMR_HOST=<emr_private_ip>
DB_ROOT_PASSWORD=<same-as-db-box>
OE_PASS=pass
EOF
aws ecr get-login-password --region <region> | docker login --username AWS --password-stdin <acct>.dkr.ecr.<region>.amazonaws.com
docker compose up -d
# first boot regenerates the autoloader (~7 min). Watch until FHIR answers:
watch -n 15 'curl -sk -o /dev/null -w "%{http_code}\n" https://localhost/apis/default/fhir/metadata'
```

**5c. Agent box**  (uses the EMR box private IP + your domain)
```bash
ssh -i <key>.pem ubuntu@<agent_public_ip>
mkdir -p ~/agent && cd ~/agent
base=https://raw.githubusercontent.com/sainathyai/openemr-base-clean/feat/clinical-copilot-foundation/deploy/boxes/agent
curl -fsSLO $base/docker-compose.yml
curl -fsSLO $base/Caddyfile
cat > .env <<EOF
AGENT_IMAGE=<acct>.dkr.ecr.<region>.amazonaws.com/copilot-agent:latest
EMR_HOST=<emr_private_ip>
COPILOT_DOMAIN=<your.subdomain>
COPILOT_CLIENT_ID=7S-fVOs3UxZ8Ma-sbDTRRDnpr36znRpyTnjniaup3Sw
COPILOT_CLIENT_SECRET=<your-oauth-client-secret>
COPILOT_DEV_USER=drhouse
COPILOT_DEV_PASS=DocPass123!
EOF
aws ecr get-login-password --region <region> | docker login --username AWS --password-stdin <acct>.dkr.ecr.<region>.amazonaws.com
docker compose up -d
docker compose logs -f caddy     # look for "certificate obtained successfully"
```

## 6. Verify

Open `https://your.subdomain` — patient picker, briefing, chat, order check, all live.
```bash
curl -s https://your.subdomain/api/health
curl -s https://your.subdomain/api/patients | head -c 200
```

---

## Operations

**Power the fleet up/down together** (from your machine, needs the deployer creds):
```bash
python deploy/scripts/fleet.py status
python deploy/scripts/fleet.py start
python deploy/scripts/fleet.py stop
```
Private IPs persist across stop/start, so the wiring never changes. One t3.micro left
running stays inside the free 750 hours; two is ~$7/mo of credits.

**Redeploy only the AI layer** (rebuild → push → pull on the agent box):
```bash
# local:
docker build -t clinical-copilot:latest copilot
AWS_REGION=<region> bash deploy/scripts/push-images.sh   # (or just re-tag+push the agent)
# agent box:
docker compose pull copilot && docker compose up -d copilot
```

**Enable real Claude** (a specific demo only): on the agent box set `COPILOT_FORCE_STUB=0`
+ `ANTHROPIC_API_KEY` + `COPILOT_MODEL=claude-haiku-4-5-20251001`, `up -d`, then revert.

**Tear down:** `terraform destroy` (in deploy/terraform), delete the DNS record, and
optionally the ECR repos.
