# Seeded MariaDB image (A). Our OpenEMR database — 20 patients, backfilled vitals,
# registered OAuth clients — baked in as an init seed plus the app-user grant.
# On first start with an empty data dir the entrypoint loads these; data then
# persists in the mounted volume. Reproducible: build -> run -> our exact DB.
FROM mariadb:11.8.8

# 01 = the gzipped openemr dump, 02 = the app user/grant (alphabetical order matters)
COPY initdb/ /docker-entrypoint-initdb.d/
