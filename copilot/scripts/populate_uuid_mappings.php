<?php
// DEV utility: populate uuid_mapping for resources created outside OpenEMR's
// normal write path (our synthetic vitals backfill, DQ-5). OpenEMR surfaces each
// vital component as its own FHIR Observation, each needing a mapped uuid.
// Run inside the openemr container.
$ignoreAuth = true;
$_GET['site'] = 'default';
$sessionAllowWrite = true;
require_once(__DIR__ . '/../../interface/globals.php');

use OpenEMR\Common\Uuid\UuidMapping;

$n = UuidMapping::createAllMissingResourceUuids();
echo "uuid_mapping rows created: {$n}\n";
