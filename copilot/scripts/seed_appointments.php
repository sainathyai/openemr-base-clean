<?php
// Seed a full clinic day into the OpenEMR calendar so the Co-Pilot's live-FHIR
// schedule (/api/schedule -> FHIR Appointment?date=today) has real records.
//
// One appointment per patient, 15-minute cadence from 08:00, provider drhouse,
// visit reason = the patient's top active problem (surfaced to FHIR as `comment`,
// which the client maps to the schedule reason). First two are marked "arrived",
// the rest "booked" -> FHIR 'arrived' ('@') / 'booked' ('*'). Idempotent: it clears
// this provider's appointments for today before inserting, so re-running is safe.
//
// Run INSIDE the OpenEMR container (has the app + DB):
//   docker cp seed_appointments.php <emr-container>:/tmp/seed_appointments.php
//   docker exec <emr-container> php /tmp/seed_appointments.php
//
// Env overrides (optional): SEED_PROVIDER, SEED_CATID, SEED_FACILITY, SEED_WEBROOT.

$ignoreAuth = true;
$_GET['site'] = 'default';
$sessionAllowWrite = true;

$webroot = getenv('SEED_WEBROOT') ?: '/var/www/localhost/htdocs/openemr';
require_once($webroot . '/interface/globals.php');

use OpenEMR\Common\Uuid\UuidRegistry;

$PROVIDER = (int)(getenv('SEED_PROVIDER') ?: 14);   // drhouse
$CATID    = (int)(getenv('SEED_CATID') ?: 5);       // Office Visit
$FACILITY = (int)(getenv('SEED_FACILITY') ?: 3);    // main facility
$today    = date('Y-m-d');
$SLOT_MIN = 15;

// Idempotent: remove any appointments this script previously seeded for today.
sqlStatement(
    "DELETE FROM openemr_postcalendar_events WHERE pc_aid = ? AND pc_eventDate = ?",
    [$PROVIDER, $today]
);

$patients = sqlStatement("SELECT pid, uuid FROM patient_data ORDER BY pid");
$i = 0; $created = 0;
while ($p = sqlFetchArray($patients)) {
    $pid = $p['pid'];

    // visit reason = most recent active *disorder*, preferred over Synthea's social
    // history / finding noise ("Educated to high school level (finding)"); strip the
    // SNOMED qualifier so it reads like a clinic reason ("Childhood asthma").
    $prob = sqlQuery(
        "SELECT title FROM lists
          WHERE pid = ? AND type = 'medical_problem' AND activity = 1
          ORDER BY (title LIKE '%(disorder)%') DESC, `date` DESC LIMIT 1",
        [$pid]
    );
    $reason = ($prob && !empty($prob['title'])) ? $prob['title'] : 'Follow-up visit';
    $reason = preg_replace('/\s*\((disorder|finding|situation|procedure)\)\s*$/i', '', $reason);

    $startTs   = strtotime("$today 08:00:00") + ($i * $SLOT_MIN * 60);
    $startTime = date('H:i:s', $startTs);
    $endTime   = date('H:i:s', $startTs + $SLOT_MIN * 60);
    $status    = ($i < 2) ? '@' : '*';   // FHIR: arrived / booked

    $uuid = (new UuidRegistry())->createUuid();

    sqlInsert(
        "INSERT INTO openemr_postcalendar_events SET
            uuid = ?, pc_pid = ?, pc_catid = ?, pc_title = ?, pc_time = NOW(),
            pc_duration = ?, pc_hometext = ?, pc_eventDate = ?, pc_endDate = ?,
            pc_apptstatus = ?, pc_startTime = ?, pc_endTime = ?, pc_facility = ?,
            pc_billing_location = ?, pc_informant = 1, pc_eventstatus = 1,
            pc_sharing = 1, pc_aid = ?",
        [
            $uuid, $pid, $CATID, $reason, $SLOT_MIN * 60, $reason, $today, $today,
            $status, $startTime, $endTime, $FACILITY, $FACILITY, $PROVIDER,
        ]
    );
    $created++; $i++;
}

// Belt-and-suspenders: ensure the FHIR-facing pc_uuid exists for every row.
UuidRegistry::createMissingUuidsForTables(['openemr_postcalendar_events']);

echo "seeded {$created} appointments for {$today} (provider {$PROVIDER}, "
   . "cat {$CATID}, facility {$FACILITY})\n";
