export function reportUrlForAudience(job, audience) {
  return audience === 'creator' ? job.creatorReportUrl : job.reportUrl;
}

export function hasAudienceReport(job) {
  return Boolean(job.reportUrl || job.creatorReportUrl);
}
