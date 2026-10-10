def fleet_task_v1:
  [
    split(",")[]
    | gsub("^[[:space:]]+|[[:space:]]+$"; "")
    | select(length > 0)
  ]
  | reduce .[] as $task ([];
      if index($task) == null then . + [$task] else . end)
  | join(",");

def fleet_taskset_supports_task_v1:
  . == "seta" or
  . == "smith" or
  . == "terminalbench21" or
  . == "sweverify" or
  . == "browsecomp" or
  . == "deepsearchqa" or
  . == "agent-fleet-swe-rebench-v2" or
  . == "waa" or . == "waa-v2" or . == "waa2" or
  . == "ale" or
  . == "pinchbench" or
  . == "clawbio" or
  . == "." or
  . == ".." or
  startswith("/") or
  startswith("./") or
  startswith("../") or
  startswith("~/");

def fleet_spec_v1:
  if type == "object" and
     ((keys - ["agent", "schema_version", "task", "taskset", "workers"]) | length == 0) and
     (.schema_version == 1) and
     (.taskset | type == "string" and length > 0 and (test("[[:cntrl:]]") | not)) and
     ((has("task") | not) or
       ((.task | type == "string" and (test("[[:cntrl:]]") | not) and
         (fleet_task_v1 | length > 0)) and
        (.taskset | fleet_taskset_supports_task_v1))) and
     ((has("agent") | not) or
       (.agent | type == "string" and length > 0 and (test("[[:cntrl:]]") | not))) and
     # Prompt mode's JSON Schema says integer, but JSON/jq represents both 3 and
     # 3.0 as numbers. Deliberately accept integral values here, then normalize
     # them so downstream shell arithmetic always receives an integer.
     ((has("workers") | not) or
       (.workers | type == "number" and . > 0 and . == floor and . <= 4096))
  then {schema_version, taskset}
    + (if has("task") then {task: (.task | fleet_task_v1)} else {} end)
    + (if has("agent") then {agent} else {} end)
    + (if has("workers") then {workers: (.workers | floor)} else {} end)
  else error("invalid FleetSpec") end;
