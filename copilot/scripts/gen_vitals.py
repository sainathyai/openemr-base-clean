import sys, random, math
from collections import defaultdict

rows = []
with open(sys.argv[1]) as f:
    for line in f:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        pid, enc, dt, h, bmi, htn = line.split("\t")
        rows.append((int(pid), int(enc), dt, float(h), float(bmi), int(htn)))

by_pid = defaultdict(list)
for r in rows:
    by_pid[r[0]].append(r)

out = []
out.append("-- Synthetic vitals backfill (BP + weight) — DQ-5 remediation.")
out.append("-- Clearly synthetic: metric units (height cm, weight kg), correlated to HTN/obesity, mild trend.")
out.append("DELETE f FROM forms f WHERE f.formdir='vitals';")
out.append("DELETE FROM form_vitals;")

for pid, recs in by_pid.items():
    recs.sort(key=lambda r: r[2])
    n = len(recs)
    random.seed(pid)  # reproducible per patient
    for i, (p, enc, dt, h_cm, base_bmi, htn) in enumerate(recs):
        progress = i / (n - 1) if n > 1 else 1.0
        h_m = h_cm / 100.0
        # Blood pressure: HTN patients start high and creep up ("not at goal"); others normal-stable.
        if htn > 0:
            sbp = 138 + 14 * progress + random.uniform(-5, 5)
            dbp = sbp * 0.58 + random.uniform(-3, 3)
        else:
            sbp = 118 + 6 * progress + random.uniform(-5, 5)
            dbp = sbp * 0.60 + random.uniform(-3, 3)
        sbp = int(max(95, min(185, round(sbp))))
        dbp = int(max(55, min(110, round(dbp))))
        # Weight from baseline BMI x height^2; obese patients slowly gain.
        base_w = base_bmi * h_m * h_m
        trend = (0.06 * progress) if base_bmi >= 30 else 0.0
        w = base_w * (1 + trend + random.uniform(-0.015, 0.015))
        w = round(w, 1)
        bmi_i = round(w / (h_m * h_m), 1)
        pulse = int(max(50, min(95, round(62 + random.uniform(-9, 12)))))
        out.append(
            "INSERT INTO form_vitals (uuid,date,pid,user,groupname,authorized,activity,bps,bpd,weight,height,pulse,BMI,BMI_status) "
            f"VALUES (UNHEX(REPLACE(UUID(),'-','')),'{dt}',{p},'admin','Default',1,1,{sbp},{dbp},{w},{h_cm},{pulse},{bmi_i},'');"
        )
        out.append("SET @fvid=LAST_INSERT_ID();")
        out.append(
            "INSERT INTO forms (date,encounter,form_name,form_id,pid,user,groupname,authorized,deleted,formdir) "
            f"VALUES ('{dt}',{enc},'Vitals',@fvid,{p},'admin','Default',1,0,'vitals');"
        )

with open(sys.argv[2], "w") as f:
    f.write("\n".join(out) + "\n")

print(f"patients={len(by_pid)} encounters={len(rows)} statements={len(out)}")
