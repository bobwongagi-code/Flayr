import {
  clearStoredReportAudience,
  readStoredReportAudience,
  reportAudienceLabel,
  storeReportAudience,
} from './components/audience-switch.js';
import { statusClass, statusLabel } from './components/job-list.js';
import { hasAudienceReport as hasAudienceReports, reportUrlForAudience } from './components/report-view.js';

(function(){
  var uploadState = {
    files: { benchmark: null, creator: null },
    urls: { benchmark: null, creator: null }
  };

  var jobs = [];
  var activeJobId = null;
  var pollTimer = null;
  var demoReportMarkup = null;
  var reportAudienceState = {
    jobId: null,
    selectedAudience: null,
    activeAudience: null,
    activeFrame: null,
    shell: null,
    transitionId: 0
  };
  function $(id){ return document.getElementById(id); }

  function formatBytes(bytes){
    if (!bytes) return '';
    var mb = bytes / (1024*1024);
    return mb >= 1 ? mb.toFixed(1) + ' MB' : (bytes/1024).toFixed(0) + ' KB';
  }

  function showToast(msg){
    $('toast-text').textContent = msg;
    $('toast').classList.add('show');
    setTimeout(function(){ $('toast').classList.remove('show'); }, 3200);
  }

  function apiJson(url, options){
    return fetch(url, options).then(function(response){
      return response.json().catch(function(){ return {}; }).then(function(body){
        if (!response.ok) throw new Error(body.error || '请求失败');
        return body;
      });
    });
  }

  function escapeHtml(value){
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function mapJob(raw){
    var rawStatus = raw.status || 'failed';
    var status = (rawStatus === 'completed' || rawStatus === 'degraded') ? 'done' :
      (rawStatus === 'failed' ? 'failed' : 'generating');
    return {
      id: raw.id,
      workspaceId: raw.workspace_id || 'local',
      name: raw.name || '未命名分析',
      market: raw.market || '未指定市场',
      status: status,
      submitted: raw.submitted || '刚刚',
      progress: Number(raw.progress || 0),
      remaining: Number(raw.estimated_remaining_seconds || 0),
      phase: raw.phase || '',
      strategyLevel: Boolean(raw.strategy_level),
      degraded: rawStatus === 'degraded',
      degradedReason: raw.degraded_reason || '',
      failReason: raw.failure_reason || '',
      reportUrl: raw.bd_report_url || '',
      legacyReportUrl: raw.report_kind === 'legacy' || (!raw.bd_report_url && raw.report_url) ? (raw.report_url || '') : '',
      creatorReportUrl: raw.creator_report_url || ''
    };
  }

  function mergeJob(job){
    for (var i=0;i<jobs.length;i++){
      if (jobs[i].id === job.id){ jobs[i] = job; return job; }
    }
    jobs.unshift(job);
    return job;
  }

  function loadJobs(){
    return apiJson('/api/jobs').then(function(body){
      jobs = (body.jobs || []).map(mapJob);
      updateNavBadge();
      if ($('view-jobs').classList.contains('active')) renderJobList();
      return jobs;
    }).catch(function(error){
      showToast('任务列表加载失败：' + error.message);
      return [];
    });
  }

  function refreshJob(id, silent){
    return apiJson('/api/jobs/' + encodeURIComponent(id)).then(function(body){
      var job = mergeJob(mapJob(body));
      updateNavBadge();
      if (activeJobId === id && $('view-report').classList.contains('active')){
        renderReportShell(job);
      }
      if ($('view-jobs').classList.contains('active')) renderJobList();
      if (job.status !== 'generating') stopPolling();
      return job;
    }).catch(function(error){
      if (!silent) showToast('任务状态获取失败：' + error.message);
      return null;
    });
  }

  function startPolling(id){
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(function(){ refreshJob(id, true); }, 30000);
  }

  function stopPolling(){
    if (pollTimer){ clearInterval(pollTimer); pollTimer = null; }
  }

  function switchView(id){
    document.querySelectorAll('.view').forEach(function(v){ v.classList.remove('active'); });
    $(id).classList.add('active');
  }

  function setActiveTab(tab){
    document.querySelectorAll('#tabnav button').forEach(function(b){
      b.classList.toggle('active', b.getAttribute('data-tab') === tab);
    });
  }

  function goTab(tab){
    if (tab === 'upload'){
      setActiveTab('upload');
      switchView('view-upload');
    } else if (tab === 'jobs'){
      setActiveTab('jobs');
      switchView('view-jobs');
      loadJobs();
    }
  }

  document.querySelectorAll('#tabnav button').forEach(function(b){
    b.addEventListener('click', function(){ goTab(b.getAttribute('data-tab')); });
  });
  $('btn-new-from-jobs').addEventListener('click', function(){ goTab('upload'); });
  $('btn-back-to-jobs').addEventListener('click', function(){ goTab('jobs'); });
  $('btn-gen-to-jobs').addEventListener('click', function(){ goTab('jobs'); });
  $('btn-fail-reupload').addEventListener('click', function(){ goTab('upload'); });
  document.querySelectorAll('#report-audience [data-audience]').forEach(function(button){
    button.addEventListener('click', function(){
      var job = findJob(activeJobId);
      if (job) loadReportAudience(job, button.getAttribute('data-audience'));
    });
  });

  function updateNavBadge(){
    var n = jobs.filter(function(j){ return j.status === 'generating'; }).length;
    var el = $('nav-badge');
    if (n > 0){ el.style.display='flex'; el.textContent = n; } else { el.style.display='none'; }
  }

  function renderJobList(){
    var listEl = $('job-list');
    if (jobs.length === 0){
      listEl.innerHTML = '<div class="card empty-jobs">还没有分析任务，去「新建分析」上传两条视频试试看。</div>';
      return;
    }
    listEl.innerHTML = '';
    jobs.forEach(function(job){
      var card = document.createElement('div');
      card.className = 'card job-card';
      card.innerHTML =
        '<div class="job-main">' +
          '<div class="job-name">' + escapeHtml(job.name) + '</div>' +
          '<div class="job-sub">' + escapeHtml(job.market) + ' · 提交于 ' + escapeHtml(job.submitted) + '</div>' +
        '</div>' +
        '<div class="job-status ' + statusClass(job) + '"><span class="dot"></span>' + escapeHtml(statusLabel(job)) + '</div>';
      card.addEventListener('click', function(){ openReport(job.id); });
      listEl.appendChild(card);
    });
    updateNavBadge();
  }

  function formatEta(seconds){
    var minutes = Math.max(1, Math.ceil(Number(seconds || 0) / 60));
    return minutes + ' 分钟';
  }

  function phaseTextFor(progress, job){
    var phase = job && job.phase;
    if (phase === '报告生成') phase = '报告生成中';
    if (!phase) phase = progress < 33 ? '素材处理与转写' : (progress < 80 ? '模型对比分析' : '报告生成中');
    var suffix = progress < 100 ? ' · 预计还需约 ' + formatEta(job && job.remaining) : '';
    return phase + suffix;
  }

  function updateReportAudienceControls(job, audience, loading){
    var availableCreator = Boolean(job.creatorReportUrl);
    var availableInternal = Boolean(job.reportUrl);
    document.querySelectorAll('#report-audience [data-audience]').forEach(function(button){
      var target = button.getAttribute('data-audience');
      var available = target === 'creator' ? availableCreator : availableInternal;
      var label = button.querySelector('.audience-label');
      label.textContent = reportAudienceLabel(target) + (available ? '' : '（未生成）');
      button.disabled = !available || Boolean(loading);
      button.classList.toggle('selected', available && audience === target);
      button.classList.toggle('is-loading', Boolean(loading) && available && audience === target);
      button.setAttribute('aria-pressed', String(available && audience === target));
      button.title = available ? '查看' + reportAudienceLabel(target) + '报告' : '该报告尚未生成';
    });

    var note = $('report-audience-note');
    if (loading){
      note.textContent = '正在打开' + reportAudienceLabel(audience) + '报告';
    } else if (audience){
      note.textContent = '当前展示：' + reportAudienceLabel(audience) + '报告';
    } else if (availableCreator && availableInternal){
      note.textContent = '请选择要查看的报告';
    } else {
      note.textContent = '请选择要查看的报告，未生成的版本不可用';
    }
  }

  function resetReportFrameState(){
    reportAudienceState.transitionId += 1;
    reportAudienceState.jobId = null;
    reportAudienceState.selectedAudience = null;
    reportAudienceState.activeAudience = null;
    reportAudienceState.activeFrame = null;
    reportAudienceState.shell = null;
  }

  function loadReportAudience(job, audience){
    var url = reportUrlForAudience(job, audience);
    if (!url || reportAudienceState.jobId !== job.id || !reportAudienceState.shell) return;
    if (reportAudienceState.activeAudience === audience && reportAudienceState.activeFrame) return;

    var shell = reportAudienceState.shell;
    var previousFrame = reportAudienceState.activeFrame;
    var emptyState = shell.querySelector('.report-frame-empty');
    var loadingNote = shell.querySelector('.report-frame-loading');
    var transitionId = ++reportAudienceState.transitionId;
    reportAudienceState.selectedAudience = audience;
    updateReportAudienceControls(job, audience, true);
    emptyState.hidden = Boolean(previousFrame);
    loadingNote.textContent = previousFrame ? '正在切换报告' : '正在打开报告';
    loadingNote.hidden = false;
    shell.setAttribute('aria-busy', 'true');

    function restorePreviousReport(){
      loadingNote.hidden = true;
      shell.setAttribute('aria-busy', 'false');
      var fallbackAudience = reportAudienceState.activeAudience;
      var retainedFrame = previousFrame && previousFrame.parentNode ? previousFrame : shell.querySelector('.report-frame-active');
      if (retainedFrame){
        retainedFrame.classList.remove('report-frame-exiting');
        retainedFrame.classList.add('report-frame-active');
        reportAudienceState.activeFrame = retainedFrame;
        reportAudienceState.selectedAudience = fallbackAudience;
        storeReportAudience(job.id, job.workspaceId, fallbackAudience);
        updateReportAudienceControls(job, fallbackAudience, false);
      } else if (fallbackAudience && reportUrlForAudience(job, fallbackAudience)) {
        reportAudienceState.activeFrame = null;
        reportAudienceState.activeAudience = null;
        reportAudienceState.selectedAudience = fallbackAudience;
        updateReportAudienceControls(job, fallbackAudience, true);
        loadReportAudience(job, fallbackAudience);
      } else {
        reportAudienceState.selectedAudience = null;
        clearStoredReportAudience(job.id, job.workspaceId);
        emptyState.hidden = false;
        updateReportAudienceControls(job, null, false);
      }
      showToast('报告打开失败，请稍后重试');
    }

    var frameReady = false;
    var frame = document.createElement('iframe');
    frame.className = 'report-frame report-frame-pending';
    frame.title = job.name + ' · ' + reportAudienceLabel(audience) + '报告';
    frame.onload = function(){
      if (!frameReady) return;
      if (transitionId !== reportAudienceState.transitionId){
        frame.remove();
        return;
      }
      var contentType = '';
      try {
        contentType = String(frame.contentDocument && frame.contentDocument.contentType || '').toLowerCase();
      } catch (error) {
        contentType = '';
      }
      if (contentType && contentType !== 'text/html' && contentType !== 'application/xhtml+xml'){
        frame.remove();
        restorePreviousReport();
        return;
      }
      frame.classList.remove('report-frame-pending');
      frame.classList.add('report-frame-active');
      emptyState.hidden = true;
      loadingNote.hidden = true;
      shell.setAttribute('aria-busy', 'false');
      reportAudienceState.activeFrame = frame;
      reportAudienceState.activeAudience = audience;
      storeReportAudience(job.id, job.workspaceId, audience);
      updateReportAudienceControls(job, audience, false);
      if (previousFrame){
        previousFrame.classList.add('report-frame-exiting');
        window.setTimeout(function(){
          if (previousFrame.parentNode) previousFrame.remove();
        }, 220);
      }
    };
    frame.onerror = function(){
      if (transitionId !== reportAudienceState.transitionId){
        frame.remove();
        return;
      }
      frame.remove();
      restorePreviousReport();
    };
    shell.appendChild(frame);
    fetch(url, {method:'HEAD', credentials:'same-origin', cache:'no-store'}).then(function(response){
      if (transitionId !== reportAudienceState.transitionId){
        frame.remove();
        return;
      }
      var contentType = String(response.headers.get('content-type') || '').toLowerCase();
      if (!response.ok || (contentType && contentType.indexOf('text/html') !== 0 && contentType.indexOf('application/xhtml+xml') !== 0)){
        throw new Error('报告不可用');
      }
      frameReady = true;
      frame.src = url;
    }).catch(function(){
      if (transitionId !== reportAudienceState.transitionId){
        frame.remove();
        return;
      }
      frame.remove();
      restorePreviousReport();
    });
  }

  function renderLegacyReport(job){
    var content = $('report-done-content');
    resetReportFrameState();
    $('report-audience').hidden = true;
    content.setAttribute('data-real-job', job.id);
    content.innerHTML = '';

    var shell = document.createElement('div');
    shell.className = 'report-frame-shell';
    var frame = document.createElement('iframe');
    frame.className = 'report-frame report-frame-active';
    frame.title = job.name + ' 提升报告';
    frame.src = job.legacyReportUrl;
    shell.appendChild(frame);
    content.appendChild(shell);
  }

  function renderActualReport(job){
    var content = $('report-done-content');
    if (content.getAttribute('data-real-job') === job.id && reportAudienceState.jobId === job.id) return;
    resetReportFrameState();
    content.setAttribute('data-real-job', job.id);
    content.innerHTML = '';

    var shell = document.createElement('div');
    shell.className = 'report-frame-shell';
    shell.setAttribute('aria-busy', 'false');
    var emptyState = document.createElement('div');
    emptyState.className = 'report-frame-empty';
    emptyState.textContent = '请选择报告用途';
    var loadingNote = document.createElement('div');
    loadingNote.className = 'report-frame-loading';
    loadingNote.hidden = true;
    shell.appendChild(emptyState);
    shell.appendChild(loadingNote);
    content.appendChild(shell);

    reportAudienceState.jobId = job.id;
    reportAudienceState.shell = shell;
    var storedAudience = readStoredReportAudience(job.id, job.workspaceId);
    if (storedAudience && reportUrlForAudience(job, storedAudience)){
      updateReportAudienceControls(job, storedAudience, false);
      loadReportAudience(job, storedAudience);
    } else {
      if (storedAudience) clearStoredReportAudience(job.id, job.workspaceId);
      updateReportAudienceControls(job, null, false);
    }
  }

  function renderDemoReport(){
    var content = $('report-done-content');
    resetReportFrameState();
    $('report-audience').hidden = true;
    content.removeAttribute('data-real-job');
    content.innerHTML = demoReportMarkup;
    buildStageTabsAndPanels();
  }

  function renderReportShell(job){
    $('report-title').textContent = job.name + ' · 提升报告';
    $('report-meta').textContent = '产品：' + job.name + ' · 市场：' + job.market;

    var levelBadge = $('report-level-badge');
    if (job.strategyLevel){
      levelBadge.textContent = '策略增强分析';
      levelBadge.className = 'badge badge-level-strategy';
    } else {
      levelBadge.textContent = '视频证据分析';
      levelBadge.className = 'badge badge-level-basic';
    }
    var hasAudienceReport = hasAudienceReports(job);
    var hasLegacyReport = Boolean(job.legacyReportUrl);
    var hasReport = hasAudienceReport || hasLegacyReport;
    var demoBadge = document.querySelector('.badge-demo');
    demoBadge.style.display = hasReport ? 'none' : 'inline-flex';
    $('report-audience').hidden = job.status !== 'done' || !hasAudienceReport;
    $('report-degraded-inline').style.display = job.degraded ? 'flex' : 'none';
    $('report-degraded-label').textContent = job.degraded
      ? '已完成（部分分析能力降级）'
      : '';
    $('report-degraded-detail').textContent = job.degraded
      ? (job.degradedReason || '部分分析能力降级，报告仍可查看。')
      : '';

    $('report-generating').style.display = job.status === 'generating' ? 'block' : 'none';
    $('report-failed').style.display = job.status === 'failed' ? 'block' : 'none';
    $('report-done-content').style.display = job.status === 'done' ? 'block' : 'none';

    if (job.status === 'failed'){
      $('fail-reason').textContent = job.failReason || '分析未能完成，请重新上传后重试。';
    }
    if (job.status === 'generating'){
      var pct = job.progress || 0;
      $('gen-progress-fill').style.width = pct + '%';
      $('gen-progress-pct').textContent = pct + '%';
      $('gen-phase-text').textContent = phaseTextFor(pct, job);
    }
    if (job.status === 'done'){
      if (hasAudienceReport) renderActualReport(job);
      else if (hasLegacyReport) renderLegacyReport(job);
      else renderDemoReport();
    }
  }

  function openReport(id){
    activeJobId = id;
    var job = findJob(id);
    if (!job) return;
    setActiveTab(null);
    renderReportShell(job);
    switchView('view-report');
    if (job.status === 'generating'){
      startPolling(id);
      refreshJob(id, true);
    } else {
      stopPolling();
    }
  }

  function findJob(id){
    for (var i=0;i<jobs.length;i++){ if (jobs[i].id === id) return jobs[i]; }
    return null;
  }

  /* ---------- upload dropzones ---------- */
  function checkReady(){
    var ready = uploadState.files.benchmark && uploadState.files.creator;
    $('btn-start').disabled = !ready;
    $('cta-hint').textContent = ready ? '两条视频已就绪 · 提交后预计约 18 分钟完成' : '请上传两条视频后开始分析';
  }

  function wireDropzone(role){
    var dz = $('dz-' + role);
    var input = $('input-' + role);
    var emptyEl = $('empty-' + role);
    var filledEl = $('filled-' + role);
    var videoEl = $('video-' + role);
    var nameEl = $('name-' + role);
    var sizeEl = $('size-' + role);

    function handleFile(file){
      if (!file || file.type.indexOf('video') === -1) return;
      uploadState.files[role] = file;
      if (uploadState.urls[role]) URL.revokeObjectURL(uploadState.urls[role]);
      var url = URL.createObjectURL(file);
      uploadState.urls[role] = url;
      videoEl.src = url;
      nameEl.textContent = file.name;
      sizeEl.textContent = formatBytes(file.size);
      emptyEl.style.display = 'none';
      filledEl.classList.add('show');
      checkReady();
    }

    emptyEl.addEventListener('click', function(){ input.click(); });
    input.addEventListener('change', function(e){ handleFile(e.target.files[0]); });

    dz.addEventListener('dragover', function(e){ e.preventDefault(); dz.classList.add('dragover'); });
    dz.addEventListener('dragleave', function(){ dz.classList.remove('dragover'); });
    dz.addEventListener('drop', function(e){
      e.preventDefault();
      dz.classList.remove('dragover');
      handleFile(e.dataTransfer.files[0]);
    });

    dz.querySelector('[data-swap]').addEventListener('click', function(){
      emptyEl.style.display = 'flex';
      filledEl.classList.remove('show');
      uploadState.files[role] = null;
      input.value = '';
      checkReady();
    });
  }
  wireDropzone('benchmark');
  wireDropzone('creator');

  function resetUploadForm(){
    ['benchmark','creator'].forEach(function(role){
      uploadState.files[role] = null;
      if (uploadState.urls[role]) URL.revokeObjectURL(uploadState.urls[role]);
      uploadState.urls[role] = null;
      $('empty-' + role).style.display = 'flex';
      $('filled-' + role).classList.remove('show');
      $('input-' + role).value = '';
    });
    $('f-product-name').value = '';
    $('f-category').value = '';
    $('f-market').value = '';
    $('f-price').value = '';
    $('f-selling-point').value = '';
    checkReady();
  }

  $('btn-start').addEventListener('click', function(){
    if ($('btn-start').disabled) return;
    var productName = $('f-product-name').value.trim() || '未命名分析';

    $('btn-start').disabled = true;
    $('btn-start-label').textContent = '提交中…';

    var form = new FormData();
    form.append('benchmark_video', uploadState.files.benchmark, uploadState.files.benchmark.name);
    form.append('creator_video', uploadState.files.creator, uploadState.files.creator.name);
    form.append('product_name', productName);
    form.append('category', $('f-category').value);
    form.append('market', $('f-market').value);
    form.append('price', $('f-price').value);
    form.append('selling_point', $('f-selling-point').value);

    apiJson('/api/jobs', { method:'POST', body:form }).then(function(raw){
      var job = mergeJob(mapJob(raw));
      updateNavBadge();
      showToast('已提交分析任务，预计约 18 分钟完成');
      resetUploadForm();
      $('btn-start-label').textContent = '开始分析';
      openReport(job.id);
    }).catch(function(error){
      $('btn-start').disabled = false;
      $('btn-start-label').textContent = '开始分析';
      showToast('提交失败：' + error.message);
    });
  });

  /* ---------- report: done content (S1-S6 tabs) ---------- */
  var stageData = [
    { code:'S1', name:'开场 Hook', sev:'small',
      creator:{ ts:'0:00–0:03', text:'特写产品包装 + 贴纸文字"亲测有效"，口播："我家宝宝也在用这款"' },
      benchmark:{ ts:'0:00–0:02', text:'孩子不肯刷牙的场景直接入镜，同步痛点口播："孩子不肯刷牙？"' },
      gap:'开场信息密度接近标杆，建议将产品露出压缩到 2 秒内以更快锚定痛点' },
    { code:'S2', name:'产品引出', sev:'medium',
      creator:{ ts:'0:03–0:07', text:'产品仅在手部出现，未展示品牌信息与正面包装' },
      benchmark:{ ts:'0:02–0:06', text:'产品正面特写 + 品牌与核心卖点字幕同步出现' },
      gap:'产品身份建立较慢，缺少清晰的品牌信息露出' },
    { code:'S3', name:'卖点演示', sev:'large',
      creator:{ ts:'0:07–0:15', text:'口播描述功效，画面为静态产品摆拍，无实际使用动作' },
      benchmark:{ ts:'0:06–0:14', text:'达人实际给孩子刷牙，展示刷头软毛接触牙龈的过程' },
      gap:'核心卖点缺少真实使用演示，证明力明显不足' },
    { code:'S4', name:'效果证明', sev:'medium',
      creator:{ ts:'0:15–0:20', text:'口播"孩子现在爱刷牙了"，无画面佐证' },
      benchmark:{ ts:'0:14–0:19', text:'孩子主动拿起牙刷 + 露齿笑镜头，效果可见' },
      gap:'效果描述缺少可见画面支撑，说服力打折' },
    { code:'S5', name:'信任背书', sev:'small',
      creator:{ ts:'0:20–0:24', text:'屏幕角标"月销 10 万+"' },
      benchmark:{ ts:'0:19–0:23', text:'权威机构检测报告截图 + 口播"儿科医生推荐"' },
      gap:'背书来源可信度可进一步加强，销量数字属弱背书' },
    { code:'S6', name:'促单转化', sev:'large',
      creator:{ ts:'0:24–0:28', text:'口播"链接在下方"，字幕"点击购买"' },
      benchmark:{ ts:'0:23–0:30', text:'价格对比 + 限时赠品 + 购物车高亮点击引导' },
      gap:'缺少促单钩子（价格 / 赠品 / 限时），CTA 力度偏弱' }
  ];

  function sevClass(sev){ return sev === 'large' ? 'sev-large' : (sev === 'medium' ? 'sev-medium' : 'sev-small'); }
  function sevLabel(sev){ return sev === 'large' ? '大' : (sev === 'medium' ? '中' : '小'); }
  function sevDotColor(sev){ return sev === 'large' ? 'var(--red)' : (sev === 'medium' ? 'var(--amber)' : 'var(--gray-chip)'); }

  function buildStageTabsAndPanels(){
    var tabsEl = $('stage-tabs');
    var panelsEl = $('stage-panels');
    tabsEl.innerHTML = '';
    panelsEl.innerHTML = '';

    stageData.forEach(function(sd, idx){
      var tab = document.createElement('button');
      tab.className = 'stage-tab' + (idx === 0 ? ' active' : '');
      tab.innerHTML = '<span class="sev-dot" style="background:' + sevDotColor(sd.sev) + '"></span>' + sd.code + ' ' + sd.name;
      tab.addEventListener('click', function(){
        document.querySelectorAll('.stage-tab').forEach(function(t){ t.classList.remove('active'); });
        document.querySelectorAll('.stage-panel').forEach(function(p){ p.classList.remove('active'); });
        tab.classList.add('active');
        $('panel-' + sd.code).classList.add('active');
      });
      tabsEl.appendChild(tab);

      var panel = document.createElement('div');
      panel.className = 'stage-panel' + (idx === 0 ? ' active' : '');
      panel.id = 'panel-' + sd.code;
      panel.innerHTML =
        '<div class="stage-panel-head">' +
          '<h3>' + sd.code + ' · ' + sd.name + '</h3>' +
          '<span class="sev-chip ' + sevClass(sd.sev) + '">差距等级：' + sevLabel(sd.sev) + '</span>' +
        '</div>' +
        '<div class="compare-grid">' +
          '<div class="compare-col creator">' +
            '<div class="who">达人表现</div>' +
            '<div class="frame-thumb"><span class="ts">' + sd.creator.ts + '</span></div>' +
            '<div class="evidence-text">' + sd.creator.text + '</div>' +
          '</div>' +
          '<div class="gap-col">' +
            '<span class="sev-chip ' + sevClass(sd.sev) + '">' + sevLabel(sd.sev) + '差距</span>' +
            '<div class="gap-text">' + sd.gap + '</div>' +
          '</div>' +
          '<div class="compare-col benchmark">' +
            '<div class="who">标杆表现</div>' +
            '<div class="frame-thumb"><span class="ts">' + sd.benchmark.ts + '</span></div>' +
            '<div class="evidence-text">' + sd.benchmark.text + '</div>' +
          '</div>' +
        '</div>';
      panelsEl.appendChild(panel);
    });
  }

  demoReportMarkup = $('report-done-content').innerHTML;
  checkReady();
  loadJobs();
  updateNavBadge();
})();
