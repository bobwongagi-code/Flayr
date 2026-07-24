const VALID_AUDIENCES = new Set(['creator', 'internal']);

function storageKey(jobId, workspaceId = 'local', environment = window.location.origin || window.location.host || 'local') {
  return ['flayr', environment, workspaceId, jobId, 'audience']
    .map(function(value){ return encodeURIComponent(String(value || 'local')); })
    .join(':');
}

export function reportAudienceStorageKey(jobId, workspaceId) {
  return storageKey(jobId, workspaceId);
}

export function readStoredReportAudience(jobId, workspaceId) {
  try {
    var value = window.localStorage.getItem(storageKey(jobId, workspaceId));
    return VALID_AUDIENCES.has(value) ? value : null;
  } catch (error) {
    return null;
  }
}

export function storeReportAudience(jobId, workspaceId, audience) {
  if (!VALID_AUDIENCES.has(audience)) return;
  try { window.localStorage.setItem(storageKey(jobId, workspaceId), audience); } catch (error) {}
}

export function clearStoredReportAudience(jobId, workspaceId) {
  try { window.localStorage.removeItem(storageKey(jobId, workspaceId)); } catch (error) {}
}

export function reportAudienceLabel(audience) {
  return audience === 'creator' ? '给达人' : '自己用';
}
