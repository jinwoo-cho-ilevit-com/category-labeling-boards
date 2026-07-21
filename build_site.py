#!/usr/bin/env python3
"""기존 board_<리뷰어>.html 4개를 카테고리별 파일 + 인덱스 메뉴 + Supabase 저장 사이트로 변환.

- 입력:  SRC_DIR/board_*.html  (각 파일은 <script id="data"> 로 전체 payload 임베드)
- 출력:  OUT_DIR/index.html + OUT_DIR/data/<rslug>/<cslug>.html
- 저장:  각 보드의 Apps Script sync -> Supabase upsert(reviews) 로 교체
- 복원:  로드 시 syncPull이 서버에서 본인 리뷰를 받아 로컬이 빈 샘플만 채움(로컬 우선)
"""
import json, re, glob, os, collections, html as _html, shutil

SRC_DIR = "/Users/jwcho/Downloads/temp"
OUT_DIR = "/Users/jwcho/Codes/category-labeling-boards"

SUPABASE_URL = "https://qnhwcwsizommxuqfpalo.supabase.co"
SUPABASE_KEY = "sb_publishable_Ss861mkQyztCl_CAtAbvmQ_ecG0fZDa"

# 진행 순서(소프트). 표시 라벨은 데이터 원본 형식. 매칭 키는 공백 제거 정규화.
TARGET_ORDER = [
    "2.1.1",
    "2.1.1 (출산/유아동)",
    "2.1.1.2 (출산/유아동)",
    "2.1.2 (출산/유아동)",
    "2.2.1 (뷰티)",
    "2.2.1 (영양제)",
]

def norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s or ""))

NORM_ORDER = [norm(c) for c in TARGET_ORDER]
CAT_SLUG = {norm(c): f"c{i+1}" for i, c in enumerate(TARGET_ORDER)}
CAT_LABEL = {norm(c): c for c in TARGET_ORDER}

DATA_RE = re.compile(r'(<script type="application/json" id="data">)(.*?)(</script>)', re.S)

# --- sync JS 교체 (Apps Script -> Supabase) --------------------------------
SYNCPUSH_RE = re.compile(
    r"function syncPush\(id\)\{if\(!SYNC\|\|!reviewer\)return;.*?"
    r"\.catch\(function\(\)\{setSyncStat\('저장 실패\(로컬 보관\)','bad'\);\}\);\}",
    re.S,
)
NEW_SYNCPUSH = (
    "function syncPush(id){if(!SB_URL||!SB_KEY||!reviewer)return;var p=noteParts(id),s=sampById[String(id)];\n"
    "    var rec={reviewer:reviewer,sample_id:String(id),"
    "tags:p.tags.join('|'),url:p.url,note:p.body,gt_candidates:(p.gtPicks||[]).join('|')};\n"
    // 샘플 메타(grp/name/gt)는 이 보드에 있는 샘플일 때만 포함 — 다른 보드에서의
    // 전체저장(syncAll)이 기존 행의 카테고리를 ''로 덮어써 진행률에서 빠지는 것 방지.
    "    if(s){rec.grp=s.group||'';rec.name=s.name||'';rec.gt=s.gt||'';}\n"
    "    fetch(SB_URL+'/rest/v1/reviews?on_conflict=reviewer,sample_id',{method:'POST',"
    "headers:{apikey:SB_KEY,Authorization:'Bearer '+SB_KEY,'Content-Type':'application/json',"
    "Prefer:'resolution=merge-duplicates,return=minimal'},body:JSON.stringify(rec)})\n"
    "      .then(function(r){if(r.ok){dirty=false;setSyncStat('저장 '+nowHM(),'ok');updateTagSum();}"
    "else{setSyncStat('저장 실패(로컬 보관)','bad');}})\n"
    "      .catch(function(){setSyncStat('저장 실패(로컬 보관)','bad');});}\n"
    # syncPull: 로드 시 서버(reviews)에서 본인 리뷰를 받아 로컬이 빈 샘플만 복원.
    # 로컬 입력이 항상 우선(덮어쓰기 없음) — 브라우저 교체/초기화 시 자동 복구용.
    "function syncPull(){if(!SB_URL||!SB_KEY||!reviewer)return;var acc=[];\n"
    "    function page(off){fetch(SB_URL+'/rest/v1/reviews?select=sample_id,tags,url,note,gt_candidates"
    "&reviewer=eq.'+encodeURIComponent(reviewer)+'&order=sample_id.asc&limit=1000&offset='+off,"
    "{headers:{apikey:SB_KEY,Authorization:'Bearer '+SB_KEY}})\n"
    "      .then(function(r){return r.ok?r.json():[];})\n"
    "      .then(function(rows){acc=acc.concat(rows);if(rows.length===1000){page(off+1000);}else{apply(acc);}})\n"
    "      .catch(function(){});}\n"
    "    function apply(rows){var n=0;rows.forEach(function(x){var id=String(x.sample_id);\n"
    "      if(!sampById[id])return;if(noteGet(id).trim()!=='')return;\n"
    "      var tags=String(x.tags||'').split('|').filter(Boolean),picks=String(x.gt_candidates||'').split('|').filter(Boolean);\n"
    "      var t=noteCombine(tags,String(x.url||''),picks,String(x.note||''));\n"
    "      if(t.trim()!==''&&noteSet(id,t))n++;});\n"
    "      if(n){dirty=false;try{refilter();}catch(e){}try{updateTagSum();}catch(e){}setSyncStat('서버에서 '+n+'건 복원','ok');}}\n"
    "    page(0);}\n"
    "setTimeout(syncPull,0);"  # 스크립트 초기화(reviewer/sampById/렌더) 완료 후 실행
)
SYNCVAR_OLD = "var SYNC=D.sync_url||'', reviewer='';"
SYNCVAR_NEW = "var SYNC=D.sync_url||'', reviewer='';var SB_URL=D.supabase_url||'',SB_KEY=D.supabase_key||'';"

CATNAV_CSS = (
    ".catnav{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:8px;"
    "font-size:13px;padding-top:8px;border-top:1px solid var(--line-soft)}"
    ".catnav a{color:var(--accent);text-decoration:none;padding:3px 9px;border:1px solid var(--line);"
    "border-radius:7px}.catnav a:hover{background:var(--surface-2)}"
    ".catnav .cn-cur{font-weight:700;color:var(--ink)}"
    ".catnav .cn-ord{font-family:var(--mono);color:var(--ink-faint)}"
)

def patch_html(raw_html: str, cat_norm: str, order_idx: int, prev_slug, next_slug) -> str:
    """뷰어 template(HTML)에 sync 교체 + 네비바 주입. 데이터는 호출부에서 이미 교체."""
    h = raw_html
    # 1) sync JS 교체
    if SYNCVAR_OLD not in h:
        raise RuntimeError("SYNC var 앵커를 못 찾음")
    h = h.replace(SYNCVAR_OLD, SYNCVAR_NEW, 1)
    h, n = SYNCPUSH_RE.subn(lambda m: NEW_SYNCPUSH, h, count=1)
    if n != 1:
        raise RuntimeError("syncPush 앵커를 못 찾음")
    # 2) CSS 주입 (첫 </style> 앞)
    h = h.replace("</style>", CATNAV_CSS + "</style>", 1)
    # 3) 네비바 주입 (chips div 뒤)
    label = CAT_LABEL[cat_norm]
    parts = ['<a href="../../index.html">◀ 전체 목록</a>',
             f'<span class="cn-ord">{order_idx+1}/{len(NORM_ORDER)}</span>',
             f'<span class="cn-cur">{_html.escape(label)}</span>']
    if prev_slug:
        parts.append(f'<a href="{prev_slug}.html">◀ 이전</a>')
    if next_slug:
        parts.append(f'<a href="{next_slug}.html">다음 ▶</a>')
    nav = '<div class="catnav">' + "".join(parts) + "</div>"
    anchor = '<div class="chips" id="chips"></div>'
    h = h.replace(anchor, anchor + "\n    " + nav, 1)
    return h

INDEX_TEMPLATE = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>레이블링 검수 보드</title>
<style>
:root{--bg:#f5f7fa;--surface:#fff;--surface-2:#eef1f6;--ink:#1a2030;--ink-soft:#586178;--ink-faint:#8a93a8;--line:#e0e5ee;--line-soft:#eaeef4;--accent:#3b6ea5;--ok:#1f8a70;--okbg:#e3f3ec;--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
@media(prefers-color-scheme:dark){:root{--bg:#0e131d;--surface:#151b28;--surface-2:#1a2130;--ink:#e6ebf4;--ink-soft:#98a3ba;--ink-faint:#6b7488;--line:#28313f;--line-soft:#202834;--accent:#6ea3d8;--ok:#48c6a2;--okbg:#12312a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;font-size:15px;line-height:1.5}
.wrap{max-width:860px;margin:0 auto;padding:24px 18px 60px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--ink-faint);font-size:13px;margin-bottom:20px}
.guide{display:inline-block;margin-bottom:20px;font-size:13px;color:var(--accent);text-decoration:none;border:1px solid var(--line);border-radius:8px;padding:7px 13px}.guide:hover{background:var(--surface-2)}
.who{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:22px}
.who .lbl{font-size:13px;color:var(--ink-soft);margin-right:4px}
.rv{font-size:14px;padding:7px 16px;border:1px solid var(--line);border-radius:9px;background:var(--surface);color:var(--ink-soft);cursor:pointer}
.rv[aria-pressed="true"]{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:700}
h2{font-size:14px;color:var(--ink-faint);text-transform:uppercase;letter-spacing:.04em;margin:26px 0 10px}
.card{display:flex;align-items:center;gap:14px;padding:13px 16px;border:1px solid var(--line);border-radius:11px;background:var(--surface);text-decoration:none;color:inherit;margin-bottom:9px}
.card:hover{border-color:var(--accent)}
.card.now{border-color:var(--accent);box-shadow:inset 3px 0 0 var(--accent)}
.card.done{opacity:.62}
.ord{font-family:var(--mono);font-size:13px;color:var(--ink-faint);width:26px;flex-shrink:0}
.cmid{flex:1;min-width:0}
.clabel{font-weight:600;font-size:15px}.clabel .nowtag{color:var(--accent);font-size:12px;margin-left:8px}
.cbarwrap{height:7px;background:var(--surface-2);border-radius:4px;margin-top:7px;overflow:hidden}
.cbar{height:100%;background:var(--accent);width:0;transition:width .3s}
.cbar.full{background:var(--ok)}
.cnum{font-family:var(--mono);font-size:13px;color:var(--ink-soft);width:96px;text-align:right;flex-shrink:0}
.cnum b{color:var(--ink)}
.hint{color:var(--ink-faint);font-size:12px}
table.team{border-collapse:collapse;width:100%;font-size:13px;margin-top:4px}
table.team th,table.team td{padding:6px 8px;border-bottom:1px solid var(--line-soft);text-align:right}
table.team th{color:var(--ink-faint);font-weight:600;font-size:11px;text-transform:uppercase}
table.team td.l,table.team th.l{text-align:left;font-family:var(--mono)}
.foot{margin-top:30px;color:var(--ink-faint);font-size:12px;border-top:1px solid var(--line-soft);padding-top:14px}
.empty{color:var(--ink-faint);padding:30px 0;text-align:center}
</style></head><body><div class="wrap">
<h1>레이블링 검수 보드</h1>
<div class="sub">카테고리를 순서대로 검수하세요. 저장은 자동으로 DB에 반영됩니다. 진행률은 GT후보 선정(없음 확정 포함) 완료 기준.</div>
<a class="guide" href="onboarding.html" target="_blank" rel="noopener">📖 GT 복수정답 검수 온보딩 가이드</a>
<div class="who" id="who"><span class="lbl">검수자</span></div>
<div id="mine"><div class="empty">위에서 본인 이름을 선택하세요.</div></div>
<div id="teamwrap" hidden><h2>팀 전체 진행률</h2><div id="team"></div></div>
<div class="foot" id="foot"></div>
</div>
<script>
var M=__MANIFEST__;
var SB=M.supabase, CATS=M.categories, RVS=M.reviewers;
function norm(s){return String(s||'').replace(/\s+/g,'');}
var NORM2SLUG={};CATS.forEach(function(c){NORM2SLUG[c.norm]=c.slug;});
var curSlug=null;
try{curSlug=localStorage.getItem('labeling_rv')||null;}catch(e){}

var whoEl=document.getElementById('who');
RVS.forEach(function(r){
  var b=document.createElement('button');b.className='rv';b.textContent=r.name;
  b.setAttribute('aria-pressed',r.slug===curSlug?'true':'false');
  b.onclick=function(){curSlug=r.slug;try{localStorage.setItem('labeling_rv',r.slug);}catch(e){}
    Array.prototype.forEach.call(whoEl.querySelectorAll('.rv'),function(x){x.setAttribute('aria-pressed','false');});
    b.setAttribute('aria-pressed','true');render();};
  whoEl.appendChild(b);
});

// done[reviewerSlug][catSlug] = Set(sample_id)
var DONE={};
function bucket(rows){
  var d={};
  rows.forEach(function(x){
    // 완료 기준: GT후보 선정됨('__none__'=없음 확정 포함). 메모/판정만 있는 행은 미완료.
    if(!String(x.gt_candidates||'').trim())return;
    var slug=NORM2SLUG[norm(x.grp)];if(!slug)return;
    // reviewer name -> slug
    var rv=RVS.filter(function(r){return r.name===x.reviewer;})[0];if(!rv)return;
    (d[rv.slug]=d[rv.slug]||{});(d[rv.slug][slug]=d[rv.slug][slug]||{});
    d[rv.slug][slug][x.sample_id]=1;
  });
  return d;
}
function cnt(rvslug,catslug){var o=DONE[rvslug]&&DONE[rvslug][catslug];return o?Object.keys(o).length:0;}

function render(){
  var mine=document.getElementById('mine');
  if(!curSlug){mine.innerHTML='<div class="empty">위에서 본인 이름을 선택하세요.</div>';return;}
  var rv=RVS.filter(function(r){return r.slug===curSlug;})[0];
  var firstIncomplete=null;
  CATS.forEach(function(c){var tot=rv.totals[c.slug]||0;if(firstIncomplete===null&&cnt(curSlug,c.slug)<tot&&tot>0)firstIncomplete=c.slug;});
  mine.innerHTML='';
  CATS.forEach(function(c,i){
    var tot=rv.totals[c.slug]||0, done=cnt(curSlug,c.slug);
    var pctv=tot?Math.round(done/tot*100):0, full=(tot>0&&done>=tot);
    var isNow=(c.slug===firstIncomplete);
    var a=document.createElement('a');
    a.className='card'+(isNow?' now':'')+(full?' done':'');
    a.href=tot? ('data/'+curSlug+'/'+c.slug+'.html') : 'javascript:void(0)';
    if(!tot)a.style.pointerEvents='none';
    a.innerHTML='<div class="ord">'+(i+1)+'</div>'+
      '<div class="cmid"><div class="clabel">'+c.label+(isNow?'<span class="nowtag">▶ 지금</span>':'')+
      (tot?'':' <span class="hint">(데이터 없음)</span>')+'</div>'+
      '<div class="cbarwrap"><div class="cbar'+(full?' full':'')+'" style="width:'+pctv+'%"></div></div></div>'+
      '<div class="cnum"><b>'+done+'</b> / '+tot+'<br>'+pctv+'%</div>';
    mine.appendChild(a);
  });
  renderTeam();
}
function renderTeam(){
  var tw=document.getElementById('teamwrap');tw.hidden=false;
  var h='<table class="team"><thead><tr><th class="l">카테고리</th>';
  RVS.forEach(function(r){h+='<th>'+r.name+'</th>';});h+='<th>합계</th></tr></thead><tbody>';
  CATS.forEach(function(c,i){
    h+='<tr><td class="l">'+(i+1)+'. '+c.label+'</td>';var sd=0,st=0;
    RVS.forEach(function(r){var tot=r.totals[c.slug]||0,done=cnt(r.slug,c.slug);sd+=done;st+=tot;
      h+='<td>'+(tot?done+'/'+tot:'–')+'</td>';});
    h+='<td><b>'+sd+'/'+st+'</b> ('+(st?Math.round(sd/st*100):0)+'%)</td></tr>';
  });
  h+='</tbody></table>';document.getElementById('team').innerHTML=h;
}

render();  // 먼저 0%로 그림
// 라이브 진행률 fetch — PostgREST 기본 상한(1000행) 대응 페이지네이션
(function loadProgress(){
  var acc=[];
  function page(off){
    fetch(SB.url+'/rest/v1/reviews?select=reviewer,sample_id,grp,gt_candidates&order=row_id.asc&limit=1000&offset='+off,
      {headers:{apikey:SB.key,Authorization:'Bearer '+SB.key}})
      .then(function(r){return r.ok?r.json():[];})
      .then(function(rows){acc=acc.concat(rows);
        if(rows.length===1000){page(off+1000);}else{DONE=bucket(acc);render();}})
      .catch(function(){document.getElementById('foot').textContent='⚠ 진행률 불러오기 실패(오프라인?) — 메뉴는 그대로 사용 가능.';});
  }
  page(0);
})();
</script></body></html>
"""


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    # 구 슬러그(c7 등) 잔존 방지: 카테고리 데이터는 매 빌드마다 새로 생성.
    # .git / onboarding.html / README.md / build_site.py 등 루트 자산은 건드리지 않음.
    shutil.rmtree(os.path.join(OUT_DIR, "data"), ignore_errors=True)
    reviewers = []  # {name, slug, totals:{catnorm:count}}
    files = sorted(glob.glob(os.path.join(SRC_DIR, "board_*.html")))
    if not files:
        raise SystemExit(f"입력 board_*.html 없음: {SRC_DIR}")
    for ri, f in enumerate(files):
        raw = open(f, encoding="utf-8").read()
        m = DATA_RE.search(raw)
        D = json.loads(m.group(2))
        reviewer = D.get("reviewer_default") or os.path.basename(f)[6:-5]
        rslug = f"r{ri+1}"
        template = raw  # 데이터는 카테고리별로 갈아끼움

        # 카테고리별 샘플 분할
        by_cat = collections.defaultdict(list)
        for s in D.get("samples", []):
            k = norm(s.get("group", ""))
            if k in CAT_SLUG:
                by_cat[k].append(s)

        rdir = os.path.join(OUT_DIR, "data", rslug)
        os.makedirs(rdir, exist_ok=True)
        totals = {}
        present = [k for k in NORM_ORDER if by_cat.get(k)]
        for oi, k in enumerate(NORM_ORDER):
            subs = by_cat.get(k, [])
            totals[k] = len(subs)
            if not subs:
                continue
            # 이 카테고리용 payload (runs 등 전역 필드는 유지, samples만 교체)
            Dc = dict(D)
            Dc["samples"] = subs
            Dc["n_samples"] = len(subs)
            Dc["embedded_samples"] = len(subs)
            Dc["sync_url"] = SUPABASE_URL          # SYNC truthy -> 검수자 입력 UI 표시
            Dc["supabase_url"] = SUPABASE_URL
            Dc["supabase_key"] = SUPABASE_KEY
            data_json = json.dumps(Dc, ensure_ascii=False).replace("<", "\\u003c")
            # prev/next: 존재하는 카테고리들 사이에서
            idx_in_present = present.index(k)
            prev_slug = CAT_SLUG[present[idx_in_present-1]] if idx_in_present > 0 else None
            next_slug = CAT_SLUG[present[idx_in_present+1]] if idx_in_present < len(present)-1 else None
            html_out = DATA_RE.sub(lambda mm: mm.group(1) + data_json + mm.group(3), template, count=1)
            html_out = patch_html(html_out, k, oi, prev_slug, next_slug)
            outp = os.path.join(rdir, CAT_SLUG[k] + ".html")
            open(outp, "w", encoding="utf-8").write(html_out)
        reviewers.append({"name": reviewer, "slug": rslug, "totals": totals})
        print(f"[{reviewer}] {rslug}: " + ", ".join(f"{CAT_SLUG[k]}={totals[k]}" for k in NORM_ORDER))

    # manifest + index
    manifest = {
        "supabase": {"url": SUPABASE_URL, "key": SUPABASE_KEY},
        "categories": [{"slug": CAT_SLUG[norm(c)], "label": c, "norm": norm(c)} for c in TARGET_ORDER],
        "reviewers": [{"name": r["name"], "slug": r["slug"],
                       "totals": {CAT_SLUG[k]: r["totals"].get(k, 0) for k in NORM_ORDER}} for r in reviewers],
    }
    open(os.path.join(OUT_DIR, "manifest.json"), "w", encoding="utf-8").write(
        json.dumps(manifest, ensure_ascii=False, indent=2))
    idx = INDEX_TEMPLATE.replace("__MANIFEST__", json.dumps(manifest, ensure_ascii=False))
    open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8").write(idx)
    print("manifest + index written. reviewers:", [r["name"] for r in reviewers])

if __name__ == "__main__":
    main()
