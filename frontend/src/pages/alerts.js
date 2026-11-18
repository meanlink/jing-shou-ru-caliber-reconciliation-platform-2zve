import {alerts} from '../api/entities.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc,fmt,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const SEG_LABEL={open:'预警中',merged:'已合并',split:'已拆分',closed:'已关闭'};
const REASON_LABEL={gap:'缺日断开',corrected:'更正回落到阈值内',superseded:'被更高修订号替代'};
const EVENT_LABEL={opened:'新建预警段',kept:'沿用原派单',merged:'合并',split:'拆分',closed:'关闭',reopened:'同区间重开'};

const DEMO_ENTRIES=[
 {channel:'合作渠道',day:'2026-11-10',revision:1,computed_net:15200,recognized_net:15020},
 {channel:'合作渠道',day:'2026-11-11',revision:1,computed_net:15100,recognized_net:14950},
 {channel:'合作渠道',day:'2026-11-12',revision:1,computed_net:15300,recognized_net:15080},
];

function tag(status){
 const cls=status==='open'?'tag tag-open':status==='closed'?'tag tag-closed':'tag tag-muted';
 return '<span class="'+cls+'">'+esc(SEG_LABEL[status]||status)+'</span>';
}

export async function alertPage(traceKey){
 if(traceKey)return traceView(traceKey);
 const [ledger,segments,inbox]=await Promise.all([
   alerts.ledger(),alerts.segments(false),alerts.inbox()]);
 const channels=[...new Set(ledger.map(r=>r.channel))];
 const openCount=segments.length;
 const unread=inbox.filter(m=>!m.read).length;
 return heading('06 / VARIANCE ALERTS','连续净收入差额预警','按渠道汇集已获批日账：每账日取最高修订号，绝对差额严格大于 100 元且连续三天起预警，缺日断开。合并/拆分沿用关联派单，仅全新段通知收件箱。',
   '<button class="button" id="refresh-alerts">刷新预警 ↻</button>')
 +'<div class="alert-stats">'+stat('现存预警段',openCount)+stat('未读通知',unread)+stat('在账账日',ledger.length)+stat('阈值规则','|差额| &gt; 100 元 · 连续 ≥3 日')+'</div>'
 +'<div class="report-grid"><section class="panel"><div class="section-title"><h2>过账一批已获批日账</h2><span>同事务生效</span></div>'
 +'<form id="alert-batch-form"><label>批次标识（幂等键）<input name="batch_id" required minlength="2" value="batch-'+Date.now().toString().slice(-6)+'"></label>'
 +'<label>来源<input name="source" value="manual" required></label>'
 +'<label>日账 JSON（channel/day/revision/computed_net/recognized_net）<textarea name="entries" rows="9" required>'+esc(JSON.stringify(DEMO_ENTRIES,null,1))+'</textarea></label>'
 +'<button type="submit" class="primary" '+(session.user.role!=='admin'?'disabled':'')+'>过账并重算预警 ↗</button>'
 +(session.user.role!=='admin'?'<p class="hint">仅业务负责人可过账；观察员与分析员可查看。</p>':'<p class="hint">日账与预警在同一事务内一起生效；相同批次标识重试不会重复派单。</p>')
 +'</form></section>'
 +'<section><div class="section-title"><h2>收件箱（仅全新段通知）</h2><span>'+inbox.length+' 条 · '+unread+' 未读</span></div><div class="schedule-list">'
 +inbox.map(m=>'<article class="'+(m.read?'':'unread')+'"><div><strong>'+esc(m.title)+'</strong><p>'+esc(m.detail)+'</p><small>'+esc(m.channel)+' · '+esc(m.start_day)+' 至 '+esc(m.end_day)+' · '+date(m.created_at)+'</small></div>'
 +(m.read?'':'<button class="button small" data-read="'+m.id+'">标记已读</button>')+'</article>').join('')
 +(!inbox.length?'<p class="empty">暂无通知。只有与历史派单无关联的全新预警段才会进入这里。</p>':'')+'</div></section></div>'
 +'<section class="panel"><div class="section-title"><h2>现存预警段与派单</h2><span>合并/拆分沿用关联派单</span></div>'
 +'<div class="table-wrap"><table><thead><tr><th>渠道</th><th>区间</th><th>天数</th><th>最大绝对差额</th><th>状态</th><th>派单键（点击追溯）</th></tr></thead><tbody>'
 +segments.map(s=>'<tr><td>'+esc(s.channel)+'</td><td>'+esc(s.start_day)+' ~ '+esc(s.end_day)+'</td><td>'+s.day_count+'</td><td>'+fmt(s.max_abs_diff)+'</td><td>'+tag(s.status)+'</td><td><a class="text-button" href="#/alerts/'+encodeURIComponent(s.dispatch_key)+'">'+esc(s.dispatch_key)+' ↗</a></td></tr>').join('')
 +(!segments.length?'<tr><td colspan="6" class="empty">当前没有达到连续三天阈值的预警段。</td></tr>':'')+'</tbody></table></div></section>'
 +'<section class="panel"><div class="section-title"><h2>渠道日账（每账日取最高修订号）</h2><span>缺日即断开</span></div>'
 +'<div class="table-wrap"><table><thead><tr><th>渠道</th><th>账日</th><th>采用修订号</th><th>统一口径净额</th><th>源账认可净额</th><th>差额</th><th>判定</th><th>历史修订</th></tr></thead><tbody>'
 +ledger.map(r=>'<tr class="'+(r.over_threshold?'row-over':'')+'"><td>'+esc(r.channel)+'</td><td>'+esc(r.day)+'</td><td>v'+r.revision+'</td><td>'+fmt(r.computed_net)+'</td><td>'+fmt(r.recognized_net)+'</td><td class="'+(r.over_threshold?'diff-over':'')+'">'+(r.diff>0?'+':'')+fmt(r.diff)+'</td><td>'+(r.over_threshold?'<span class="tag tag-open">超阈值</span>':'<span class="tag tag-muted">阈值内</span>')+'</td><td>'+(r.superseded_note?'<span class="tag tag-closed">'+esc(REASON_LABEL[r.superseded_note])+' v'+r.superseded_revisions.join('/v')+'</span>':'—')+'</td></tr>').join('')
 +(!ledger.length?'<tr><td colspan="8" class="empty">还没有日账。先在左侧过账一批。</td></tr>':'')+'</tbody></table></div></section>';
}

async function traceView(traceKey){
 const key=decodeURIComponent(traceKey);
 const t=await alerts.trace(key);
 const d=t.dispatch;
 return heading('06 / TRACE','派单追溯与现存区间','从原派单回查当时账日区间、谱系事件，以及已消失账日的原因。','<a class="button" href="#/alerts">← 返回预警台</a>')
 +'<section class="panel"><div class="section-title"><h2>原派单</h2><span>派单号 #'+d.id+'</span></div><div class="trace-grid">'
 +traceItem('渠道',d.channel)+traceItem('当时账日区间',d.start_day+' ~ '+d.end_day)+traceItem('派单键',d.dispatch_key)+traceItem('创建时间',date(d.created_at))+'</div></section>'
 +'<section class="panel"><div class="section-title"><h2>现存区间（同一派单键）</h2><span>'+t.current_segments.length+' 段</span></div><div class="table-wrap"><table><thead><tr><th>区间</th><th>天数</th><th>状态</th></tr></thead><tbody>'
 +t.current_segments.map(s=>'<tr><td>'+esc(s.start_day)+' ~ '+esc(s.end_day)+'</td><td>'+s.day_count+'</td><td>'+tag(s.status)+'</td></tr>').join('')
 +(!t.current_segments.length?'<tr><td colspan="3" class="empty">该派单当前已无现存预警段（见下方关闭事件与原因）。</td></tr>':'')+'</tbody></table></div></section>'
 +'<section class="panel"><div class="section-title"><h2>谱系时间线</h2><span>合并/拆分/关闭留痕</span></div><ol class="timeline trace-timeline">'
 +t.timeline.map(e=>'<li><div><strong>'+esc(EVENT_LABEL[e.event]||e.event)+'</strong> <span>'+esc(e.start_day)+' ~ '+esc(e.end_day)+'</span></div><div class="trace-meta">'
 +(e.lost_reason?'<span class="tag tag-closed">'+esc(REASON_LABEL[e.lost_reason]||e.lost_reason)+'</span>':'')
 +(e.closed_days.length?'<span>消失账日：'+e.closed_days.map(esc).join('、')+'</span>':'<span class="muted">无消失账日</span>')
 +'<span>'+date(e.at)+'</span></div></li>').join('')
 +(!t.timeline.length?'<li class="empty">暂无谱系事件。</li>':'')+'</ol></section>';
}

function stat(label,value){
 return '<div class="stat-card"><span>'+esc(label)+'</span><strong>'+esc(value)+'</strong></div>';
}
function traceItem(label,value){
 return '<div><span>'+esc(label)+'</span><strong>'+esc(value)+'</strong></div>';
}

export function bindAlerts(refresh){
 document.querySelector('#refresh-alerts')?.addEventListener('click',refresh);
 const form=document.querySelector('#alert-batch-form');
 if(form)submit(form,async f=>{
   let entries;
   try{entries=JSON.parse(f.get('entries'));}
   catch{throw new Error('日账 JSON 解析失败，请检查格式');}
   if(!Array.isArray(entries)||!entries.length)throw new Error('日账至少包含一条记录');
   const r=await alerts.postBatch({batch_id:f.get('batch_id'),source:f.get('source'),entries});
   notify(r.deduplicated?'批次已存在，按幂等忽略，未重复派单':('过账完成，新增派单 '+r.new_dispatches+' 条'));
   await refresh();
 });
 document.querySelectorAll('[data-read]').forEach(b=>b.onclick=async()=>{
   try{await alerts.markRead(b.dataset.read);await refresh();}catch(e){notify(e.message,true);}
 });
}
