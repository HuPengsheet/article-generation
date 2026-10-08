const $ = id => document.getElementById(id);
const state = {batch: '', papers: [], page: 1, status: null, lastJob: ''};
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const labels = {pending:'待选择',selected:'已选中',skipped:'已跳过',later:'稍后看'};
function showError(message) {$('error').textContent = message; $('error').classList.toggle('hidden', !message);}
async function api(url, data) {
  const response = await fetch(url, data === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `请求失败：${response.status}`);
  return result;
}
async function loadPapers() {
  state.papers = state.batch ? (await api('/api/papers?batch=' + encodeURIComponent(state.batch))).papers : [];
  render();
}
async function refresh() {
  const status = await api('/api/status');
  state.status = status;
  const batches = status.batches;
  if (!state.batch && batches.length) state.batch = batches[0].id;
  $('batch').innerHTML = batches.length ? batches.map(b => `<option value="${escapeHTML(b.id)}">${escapeHTML(b.date)} · ${b.counts.total} 篇</option>`).join('') : '<option value="">尚无论文</option>';
  $('batch').value = state.batch;
  const batch = batches.find(b => b.id === state.batch);
  for (const key of ['total','keywords','ai_keep','selected','articles']) $('count-' + key).textContent = `${batch?.counts[key] || 0} 篇`;
  $('model-status').textContent = status.llm_configured ? `模型：${status.model}` : '模型待配置 · .env';
  const job = status.jobs[0];
  const busy = job && ['queued','running'].includes(job.status);
  document.querySelectorAll('[data-action]').forEach(button => {button.disabled = !!busy || (button.dataset.action !== 'collect' && !state.batch) || (['ai','generate'].includes(button.dataset.action) && !status.llm_configured);});
  $('job').classList.toggle('hidden', !job);
  if (job) $('job').textContent = `${busy ? '进行中' : job.status === 'error' ? '任务有错误' : '已完成'} · ${job.message}` + (job.errors?.length ? '\n' + job.errors.map(e => `${e.id}：${e.error}`).join('\n') : '');
  $('export').classList.toggle('hidden', !state.batch);
  $('export').href = '/api/export?batch=' + encodeURIComponent(state.batch);
  const signature = job ? `${job.id}:${job.status}` : '';
  if (signature !== state.lastJob || !state.papers.length) {
    state.lastJob = signature;
    if (!busy) await loadPapers();
  }
}
function filtered() {
  const stage = $('stage').value, query = $('search').value.trim().toLowerCase();
  const papers = state.papers.filter(s => {
    const ai = s.ai, keyword = s.keyword_filter?.passed;
    const match = stage === 'all' ||
      (stage === 'candidate' && ai && ['keep','uncertain'].includes(ai.decision) && ['pending','later'].includes(s.review)) ||
      (stage === 'keywords' && keyword) ||
      (stage === 'ai_pending' && keyword && !ai) ||
      (stage === 'rejected' && ai?.decision === 'reject') ||
      (['selected','later','skipped'].includes(stage) && s.review === stage) ||
      (stage === 'articles' && s.article);
    return match && (!query || `${s.paper.id} ${s.paper.title} ${s.paper.abstract}`.toLowerCase().includes(query));
  });
  return papers.sort((a,b) => $('sort').value === 'score' ? (b.ai?.relevance_score ?? -1) - (a.ai?.relevance_score ?? -1) : a.paper.id.localeCompare(b.paper.id));
}
function render() {
  const papers = filtered(), totalPages = Math.max(1, Math.ceil(papers.length / 20));
  state.page = Math.min(state.page, totalPages);
  $('result-count').textContent = `${papers.length} 篇`;
  $('page-label').textContent = `${state.page} / ${totalPages}`;
  $('previous').disabled = state.page <= 1; $('next').disabled = state.page >= totalPages;
  if (!papers.length) {
    const message = !state.batch ? '先采集一天的全部论文。' : $('stage').value === 'candidate' ? '暂无人工候选。请先运行 AI 筛选，或切换到“关键词通过”直接查看。' : '当前条件下没有论文。';
    $('papers').innerHTML = `<div class="empty">${message}</div>`; return;
  }
  $('papers').innerHTML = papers.slice((state.page - 1) * 20, state.page * 20).map(s => {
    const p = s.paper, ai = s.ai, kw = s.keyword_filter;
    const hits = [...(kw?.strong_hits || []), ...(kw?.contextual_hits || [])].map(h => h.keyword).join(', ');
    const decision = ai ? {keep:'AI 推荐',uncertain:'AI 待确认',reject:'AI 排除'}[ai.decision] : 'AI 未处理';
    // URLs are constructed from trusted collector IDs, not HTML returned by a model.
    const url = 'https://arxiv.org/abs/' + encodeURIComponent(p.version_id).replace('%2F','/');
    return `<article class="card ${s.review === 'selected' ? 'selected' : ''}" data-id="${escapeHTML(p.id)}">
      <div class="meta"><span>${escapeHTML(p.id)}</span>${p.categories.map(c => `<span class="tag">${escapeHTML(c)}</span>`).join('')}<span class="tag">${decision}${ai ? ' · ' + ai.relevance_score : ''}</span><span class="review-label">${labels[s.review]}</span>${s.article ? '<span class="tag">草稿已生成</span>' : ''}</div>
      <h2><a href="${url}" target="_blank" rel="noopener noreferrer">${escapeHTML(p.title)}</a></h2>
      <div class="meta">${escapeHTML(p.authors)}</div>
      ${ai ? `<p class="summary">${escapeHTML(ai.summary_zh)}</p><p class="reason">${escapeHTML(ai.reason)}</p>` : ''}
      <details><summary>原始摘要与筛选依据</summary><p>${escapeHTML(p.abstract)}</p><p>关键词：${escapeHTML(hits || '未命中')} · ${escapeHTML(kw?.reason || '')}</p>${ai ? `<p>AI 证据：${escapeHTML(ai.evidence.join('；'))}</p><p>不确定性：${escapeHTML(ai.limitations)}</p>` : ''}</details>
      ${s.ai_error || s.article_error ? `<p class="error">${escapeHTML(s.ai_error || s.article_error)}</p>` : ''}
      <div class="review-bar"><select class="review" aria-label="人工选择">${Object.entries(labels).map(([key,label]) => `<option value="${key}" ${s.review === key ? 'selected' : ''}>${label}</option>`).join('')}</select><textarea class="angle" aria-label="写作角度" maxlength="3000" placeholder="可选：希望文章重点讲什么？">${escapeHTML(s.angle)}</textarea><button class="save">保存选择</button>${s.article ? '<button class="article">查看文章</button>' : ''}<span class="save-status" role="status"></span></div>
    </article>`;
  }).join('');
}
document.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', async () => {
  showError('');
  try {await api('/api/jobs', {action:button.dataset.action,batch:state.batch}); await refresh();} catch(e) {showError(e.message);}
}));
$('batch').addEventListener('change', async () => {state.batch = $('batch').value; state.page = 1; try {await loadPapers(); await refresh();} catch(e) {showError(e.message);}});
for (const id of ['stage','search','sort']) $(id).addEventListener(id === 'search' ? 'input' : 'change', () => {state.page = 1; render();});
$('previous').onclick = () => {state.page--; render();}; $('next').onclick = () => {state.page++; render();};
$('papers').addEventListener('click', async event => {
  const card = event.target.closest('.card'); if (!card) return;
  const id = card.dataset.id;
  try {
    if (event.target.classList.contains('save')) {
      const review = card.querySelector('.review').value, angle = card.querySelector('.angle').value;
      await api('/api/review', {batch:state.batch,id,review,angle});
      Object.assign(state.papers.find(s => s.paper.id === id), {review,angle});
      card.querySelector('.save-status').textContent = '已保存';
      card.querySelector('.review-label').textContent = labels[review];
      card.classList.toggle('selected', review === 'selected');
      await refresh();
    } else if (event.target.classList.contains('article')) {
      const result = await api(`/api/article?batch=${encodeURIComponent(state.batch)}&id=${encodeURIComponent(id)}`);
      $('article-text').textContent = result.article.markdown;
      $('preview-article').href = `/api/article/preview?batch=${encodeURIComponent(state.batch)}&id=${encodeURIComponent(id)}`;
      $('download-article').href = `/api/export?kind=markdown&batch=${encodeURIComponent(state.batch)}&id=${encodeURIComponent(id)}`;
      $('article-dialog').showModal();
    }
  } catch(e) {showError(e.message);}
});
$('close-article').onclick = () => $('article-dialog').close();
refresh().catch(e => showError(e.message));
setInterval(() => refresh().catch(e => showError(e.message)), 3000);
