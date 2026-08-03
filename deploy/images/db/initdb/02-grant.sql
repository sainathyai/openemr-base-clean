-- The app connects as openemr/openemr. mysqldump --databases carries the data but
-- not the account, so (re)create it with a wildcard host (the security group is the
-- real network guard on the deployed DB box).
CREATE USER IF NOT EXISTS 'openemr'@'%' IDENTIFIED BY 'openemr';
GRANT ALL PRIVILEGES ON openemr.* TO 'openemr'@'%';
FLUSH PRIVILEGES;
