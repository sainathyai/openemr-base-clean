# OpenEMR image (B1). Stock flex base (the 8.2.0-dev runtime that matches our schema
# 541) + our assembled htdocs baked in: source + built vendor/assets/themes/sites,
# minus node_modules (build-time only). Runs self-contained -- no source mount, no
# rebuild on the box. The baked sqlconf keeps $host='mysql'; compose maps the name
# 'mysql' to the DB box (service name locally, extra_hosts on the EMR box).
FROM openemr/openemr:flex@sha256:1d2c8345a320cf2985c9d02138951267739ec498559ca724b3d5397e4568482e

COPY context/openemr-htdocs.tgz /tmp/
RUN tar xzf /tmp/openemr-htdocs.tgz -C /var/www/localhost/htdocs \
 && rm /tmp/openemr-htdocs.tgz \
 && chown -R apache:apache /var/www/localhost/htdocs/openemr

# CMD inherited from the flex base (./openemr.sh)
