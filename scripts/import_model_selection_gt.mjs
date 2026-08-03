#!/usr/bin/env node
/**
 * Import the human-edited new12 GT workbook without exposing model output to
 * the annotator. The workbook is the human source of truth; this command only
 * validates it and emits a machine-readable calibration snapshot.
 *
 * Runtime requirement: execute with the workspace Node runtime that provides
 * @oai/artifact-tool.
 */

import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const STAGES = new Set(["S1", "S2", "S3", "S4", "S5", "S6"]);
const STAGE_ORDER = ["S1", "S2", "S3", "S4", "S5", "S6"];
const ROLES = new Map([["标杆", "benchmark"], ["达人", "creator"], ["benchmark", "benchmark"], ["creator", "creator"]]);
const OBSERVATION_STATES = new Set(["明确做到", "部分做到", "明确缺失", "无法判断", "不适用"]);
const IMPORTANCE_VALUES = new Set(["核心必抽", "重要", "补充"]);
const CLARITY_VALUES = new Set(["清晰", "部分清晰", "不确定"]);
const BOOLEAN_VALUES = new Set(["是", "否"]);

function parseArgs(argv) {
  const args = {};
  for (let index = 2; index < argv.length; index += 1) {
    const token = argv[index];
    if (!token.startsWith("--")) throw new Error(`unexpected argument: ${token}`);
    const key = token.slice(2).replaceAll("-", "_");
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) throw new Error(`missing value for --${key}`);
    args[key] = value;
    index += 1;
  }
  if (!args.input || !args.output || !args.manifest) {
    throw new Error("usage: import_model_selection_gt.mjs --input workbook.xlsx --manifest manifest.json --output gt.json");
  }
  return args;
}

function text(value) {
  return value == null ? "" : String(value).trim();
}

function finiteNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = typeof value === "number" ? value : Number(String(value).trim());
  return Number.isFinite(number) ? number : null;
}

function dateText(value) {
  if (value instanceof Date && !Number.isNaN(value.getTime())) return value.toISOString().slice(0, 10);
  return text(value);
}

function headerIndex(row) {
  const result = new Map();
  row.forEach((value, index) => {
    const key = text(value);
    if (key) result.set(key, index);
  });
  return result;
}

function cell(row, headers, name) {
  const index = headers.get(name);
  return index === undefined ? null : row[index];
}

function addError(errors, location, message) {
  errors.push(`${location}: ${message}`);
}

function addWarning(warnings, location, message) {
  warnings.push(`${location}: ${message}`);
}

function parseStage(value) {
  const match = text(value).toUpperCase().match(/^(S[1-6])(?:\s|$)/);
  return match && STAGES.has(match[1]) ? match[1] : "";
}

function parseStageList(value) {
  const raw = text(value);
  if (!raw) return [];
  return [...new Set(raw.split(/[、,，;；\s]+/).map((item) => item.toUpperCase()).filter(Boolean))];
}

function validateTimeRange(startValue, endValue, duration, location, errors, warnings, required) {
  const startBlank = startValue === null || startValue === undefined || text(startValue) === "";
  const endBlank = endValue === null || endValue === undefined || text(endValue) === "";
  if (startBlank && endBlank) {
    if (required) addError(errors, location, "核心事实必须填写起止时间");
    else addWarning(warnings, location, "没有填写时间，后续只能做人工语义匹配");
    return null;
  }
  if (startBlank || endBlank) {
    addError(errors, location, "起点和终点必须同时填写");
    return null;
  }
  const start = finiteNumber(startValue);
  const end = finiteNumber(endValue);
  if (start === null || end === null) {
    addError(errors, location, "时间必须是有限数字");
    return null;
  }
  if (start < 0 || end < 0 || end < start) {
    addError(errors, location, "时间必须满足 0 <= start <= end");
    return null;
  }
  if (duration !== null && end > duration + 0.25) {
    addError(errors, location, `终点 ${end} 超过视频时长 ${duration}`);
    return null;
  }
  return [Number(start.toFixed(3)), Number(end.toFixed(3))];
}

async function sha256(filePath) {
  const bytes = await fs.readFile(filePath);
  return crypto.createHash("sha256").update(bytes).digest("hex");
}

function expectedObservationKeys(sampleIds) {
  const keys = new Set();
  for (const sampleId of sampleIds) {
    for (const role of ["benchmark", "creator"]) {
      for (const stage of STAGE_ORDER) keys.add(`${sampleId}:${role}:${stage}`);
    }
  }
  return keys;
}

async function main() {
  const args = parseArgs(process.argv);
  const inputPath = path.resolve(args.input);
  const outputPath = path.resolve(args.output);
  const manifestPath = path.resolve(args.manifest);
  const [manifest, workbookBlob, workbookHash, manifestHash] = await Promise.all([
    fs.readFile(manifestPath, "utf8").then((value) => JSON.parse(value)),
    FileBlob.load(inputPath),
    sha256(inputPath),
    sha256(manifestPath),
  ]);
  const workbook = await SpreadsheetFile.importXlsx(workbookBlob);
  const sampleRows = Array.isArray(manifest.samples) ? manifest.samples : [];
  const sampleMap = new Map(sampleRows.map((sample) => [text(sample.sample_id), sample]));
  const sampleIds = [...sampleMap.keys()].filter(Boolean);
  if (sampleIds.length !== 12) throw new Error(`manifest must contain 12 samples, got ${sampleIds.length}`);

  const errors = [];
  const warnings = [];
  const stageSheet = workbook.worksheets.getItem("阶段观察");
  const factSheet = workbook.worksheets.getItem("关键事实");
  const stageValues = stageSheet.getRange("A5:M149").values;
  const factValues = factSheet.getRange("A5:O245").values;
  const stageHeaders = headerIndex(stageValues[0] || []);
  const factHeaders = headerIndex(factValues[0] || []);
  const requiredStageHeaders = ["sample_id", "视频角色", "阶段", "视频时长(秒)", "状态", "证据起点(秒)", "证据终点(秒)", "你看到/听到的现象", "判断理由/备注", "填写人", "填写日期"];
  const requiredFactHeaders = ["记录编号", "sample_id", "视频角色", "视频时长(秒)", "事实起点(秒)", "事实终点(秒)", "你看到/听到的事实", "主要阶段", "辅助阶段(可空)", "重要性", "清晰度", "应作为模型证据", "备注"];
  for (const name of requiredStageHeaders) if (!stageHeaders.has(name)) addError(errors, "阶段观察", `缺列 ${name}`);
  for (const name of requiredFactHeaders) if (!factHeaders.has(name)) addError(errors, "关键事实", `缺列 ${name}`);
  if (errors.length) throw new Error(errors.join("\n"));

  const expectedKeys = expectedObservationKeys(sampleIds);
  const seenKeys = new Set();
  const stageObservations = [];
  for (let rowIndex = 1; rowIndex < stageValues.length; rowIndex += 1) {
    const row = stageValues[rowIndex];
    const location = `阶段观察第 ${rowIndex + 5} 行`;
    const sampleId = text(cell(row, stageHeaders, "sample_id"));
    if (!sampleId) {
      addError(errors, location, "缺 sample_id");
      continue;
    }
    const sample = sampleMap.get(sampleId);
    if (!sample) {
      addError(errors, location, `未知 sample_id ${sampleId}`);
      continue;
    }
    const role = ROLES.get(text(cell(row, stageHeaders, "视频角色")));
    const stage = parseStage(cell(row, stageHeaders, "阶段"));
    const state = text(cell(row, stageHeaders, "状态"));
    if (!role) addError(errors, location, "视频角色必须是标杆或达人");
    if (!stage) addError(errors, location, "阶段必须是 S1-S6");
    if (!OBSERVATION_STATES.has(state)) addError(errors, location, "状态未选择或不在允许范围");
    if (!role || !stage || !OBSERVATION_STATES.has(state)) continue;
    const key = `${sampleId}:${role}:${stage}`;
    if (seenKeys.has(key)) addError(errors, location, `重复阶段观察 ${key}`);
    seenKeys.add(key);
    const duration = finiteNumber(cell(row, stageHeaders, "视频时长(秒)"));
    const timeRange = validateTimeRange(
      cell(row, stageHeaders, "证据起点(秒)"),
      cell(row, stageHeaders, "证据终点(秒)"),
      duration,
      location,
      errors,
      warnings,
      false,
    );
    const observedText = text(cell(row, stageHeaders, "你看到/听到的现象"));
    const rationale = text(cell(row, stageHeaders, "判断理由/备注"));
    if (!observedText) addError(errors, location, "请填写你看到/听到的现象");
    if ((state === "不适用" || state === "无法判断") && !rationale) addError(errors, location, `${state} 必须填写理由`);
    stageObservations.push({
      sample_id: sampleId,
      role,
      stage,
      state,
      time_range: timeRange,
      observed_text: observedText,
      rationale,
      annotator: text(cell(row, stageHeaders, "填写人")),
      annotation_date: dateText(cell(row, stageHeaders, "填写日期")),
    });
  }
  for (const key of expectedKeys) if (!seenKeys.has(key)) addError(errors, "阶段观察", `缺少 ${key}`);

  const keyFacts = [];
  const seenFactIds = new Set();
  const coreFactCounts = new Map();
  for (let rowIndex = 1; rowIndex < factValues.length; rowIndex += 1) {
    const row = factValues[rowIndex];
    const location = `关键事实第 ${rowIndex + 5} 行`;
    const factId = text(cell(row, factHeaders, "记录编号"));
    const description = text(cell(row, factHeaders, "你看到/听到的事实"));
    const editableValues = [
      cell(row, factHeaders, "sample_id"), cell(row, factHeaders, "产品"), cell(row, factHeaders, "视频角色"),
      cell(row, factHeaders, "视频文件"), cell(row, factHeaders, "视频时长(秒)"), cell(row, factHeaders, "事实起点(秒)"),
      cell(row, factHeaders, "事实终点(秒)"), description, cell(row, factHeaders, "主要阶段"),
      cell(row, factHeaders, "辅助阶段(可空)"), cell(row, factHeaders, "重要性"), cell(row, factHeaders, "清晰度"),
      cell(row, factHeaders, "应作为模型证据"), cell(row, factHeaders, "备注"),
    ];
    if (!editableValues.some((value) => text(value))) continue;
    if (!factId) addError(errors, location, "缺记录编号");
    if (factId && seenFactIds.has(factId)) addError(errors, location, `重复记录编号 ${factId}`);
    if (factId) seenFactIds.add(factId);
    const sampleId = text(cell(row, factHeaders, "sample_id"));
    const sample = sampleMap.get(sampleId);
    const role = ROLES.get(text(cell(row, factHeaders, "视频角色")));
    const stage = text(cell(row, factHeaders, "主要阶段")).toUpperCase();
    const secondaryStages = parseStageList(cell(row, factHeaders, "辅助阶段(可空)"));
    const importance = text(cell(row, factHeaders, "重要性"));
    const clarity = text(cell(row, factHeaders, "清晰度"));
    const shouldExtract = text(cell(row, factHeaders, "应作为模型证据"));
    if (!sample) addError(errors, location, `未知或缺失 sample_id ${sampleId}`);
    if (!role) addError(errors, location, "视频角色必须是标杆或达人");
    if (!STAGES.has(stage)) addError(errors, location, "主要阶段必须是 S1-S6");
    if (!description) addError(errors, location, "缺事实描述");
    if (!IMPORTANCE_VALUES.has(importance)) addError(errors, location, "重要性未选择");
    if (!CLARITY_VALUES.has(clarity)) addError(errors, location, "清晰度未选择");
    if (!BOOLEAN_VALUES.has(shouldExtract)) addError(errors, location, "应作为模型证据必须选择是或否");
    for (const secondaryStage of secondaryStages) if (!STAGES.has(secondaryStage)) addError(errors, location, `辅助阶段非法 ${secondaryStage}`);
    const duration = finiteNumber(cell(row, factHeaders, "视频时长(秒)"));
    const isCore = importance === "核心必抽" && shouldExtract === "是" && clarity !== "不确定";
    const timeRange = validateTimeRange(
      cell(row, factHeaders, "事实起点(秒)"),
      cell(row, factHeaders, "事实终点(秒)"),
      duration,
      location,
      errors,
      warnings,
      isCore,
    );
    if (sample && role && isCore) {
      const countKey = `${sampleId}:${role}`;
      coreFactCounts.set(countKey, (coreFactCounts.get(countKey) || 0) + 1);
    }
    keyFacts.push({
      id: factId,
      sample_id: sampleId,
      role,
      stage,
      secondary_stages: secondaryStages,
      time_range: timeRange,
      description,
      importance,
      clarity,
      should_extract: shouldExtract === "是",
      eligible_for_core_recall: isCore,
      note: text(cell(row, factHeaders, "备注")),
    });
  }
  for (const sampleId of sampleIds) {
    for (const role of ["benchmark", "creator"]) {
      if (!coreFactCounts.has(`${sampleId}:${role}`)) {
        addWarning(warnings, "关键事实", `${sampleId}/${role} 没有清晰的核心必抽事实；请确认是未填写还是视频确实没有可抽事实`);
      }
    }
  }

  if (errors.length) {
    console.error(JSON.stringify({ valid: false, errors, warnings }, null, 2));
    process.exitCode = 2;
    return;
  }
  const snapshot = {
    schema_version: 1,
    evaluation_role: "model_calibration",
    promotion_eligible: false,
    source_workbook: path.basename(inputPath),
    source_workbook_sha256: workbookHash,
    manifest_path: manifestPath,
    manifest_sha256: manifestHash,
    sample_ids: sampleIds,
    annotation_contract: {
      stage_states: [...OBSERVATION_STATES],
      key_fact_fields: ["id", "sample_id", "role", "stage", "secondary_stages", "time_range", "description", "importance", "clarity", "should_extract"],
      core_recall_definition: "importance=核心必抽 AND should_extract=true AND clarity != 不确定",
    },
    stage_observations: stageObservations,
    key_facts: keyFacts,
    summary: {
      stage_observation_rows: stageObservations.length,
      stage_uncertain_rows: stageObservations.filter((row) => row.state === "无法判断").length,
      stage_not_applicable_rows: stageObservations.filter((row) => row.state === "不适用").length,
      key_fact_rows: keyFacts.length,
      core_fact_rows: keyFacts.filter((row) => row.eligible_for_core_recall).length,
      warning_count: warnings.length,
    },
    warnings,
  };
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await fs.writeFile(outputPath, `${JSON.stringify(snapshot, null, 2)}\n`, "utf8");
  console.log(JSON.stringify({ valid: true, output: outputPath, summary: snapshot.summary, warnings }, null, 2));
}

await main();
