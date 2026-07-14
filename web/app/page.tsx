"use client";

import { useEffect, useMemo, useState } from "react";

type Health = {
  ok: boolean;
  loaded: boolean;
  device: string;
  semantic_model: string;
  visual_model: string;
  database: {
    online: boolean;
    collection: string;
    stored_points: number;
  };
};

type SearchResult = {
  index: number;
  rank: number;
  filename: string;
  score: number;
  semantic_score: number;
  visual_score: number | null;
  phash_distance: number | null;
  width: number;
  height: number;
  cache_hit: boolean;
};

type CacheStats = {
  hits: number;
  misses: number;
  stored: number;
};

const API = typeof window === "undefined"
  ? "http://127.0.0.1:8000"
  : `${window.location.protocol}//${window.location.hostname}:8000`;

export default function Home() {
  const [files, setFiles] = useState<File[]>([]);
  const [mode, setMode] = useState<"text" | "image">("text");
  const [query, setQuery] = useState("城市夜景中的人物特写");
  const [reference, setReference] = useState(0);
  const [semanticWeight, setSemanticWeight] = useState(58);
  const [results, setResults] = useState<SearchResult[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [elapsed, setElapsed] = useState<number | null>(null);
  const [cacheStats, setCacheStats] = useState<CacheStats | null>(null);

  const previews = useMemo(() => files.map((file) => URL.createObjectURL(file)), [files]);

  useEffect(() => {
    fetch(`${API}/api/health`).then((response) => response.json()).then(setHealth).catch(() => setHealth(null));
  }, []);

  useEffect(() => () => previews.forEach(URL.revokeObjectURL), [previews]);

  function chooseFiles(list: FileList | null) {
    if (!list) return;
    const selected = Array.from(list).filter((file) => file.type.startsWith("image/")).slice(0, 200);
    setFiles(selected);
    setReference(0);
    setResults([]);
    setCacheStats(null);
    setError("");
  }

  function changeMode(nextMode: "text" | "image") {
    setMode(nextMode);
    setResults([]);
    setCacheStats(null);
    setError("");
  }

  async function runSearch() {
    if (!files.length) return setError("请先选择一组图片");
    if (mode === "text" && !query.trim()) return setError("请输入想搜索的画面描述");
    setBusy(true);
    setError("");
    const data = new FormData();
    files.forEach((file) => data.append("files", file));
    data.append("mode", mode);
    data.append("text", query);
    data.append("query_index", String(reference));
    data.append("semantic_weight", String(semanticWeight / 100));
    data.append("visual_weight", String((100 - semanticWeight) / 100));
    try {
      const response = await fetch(`${API}/api/search`, { method: "POST", body: data });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "检索失败");
      setResults(payload.results);
      setElapsed(payload.inference_ms);
      setCacheStats({ hits: payload.cache_hits, misses: payload.cache_misses, stored: payload.stored_points });
      const status = await fetch(`${API}/api/health`).then((item) => item.json());
      setHealth(status);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法连接本地模型服务");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main>
      <header className="topbar">
        <div className="brand"><span className="brandMark">F</span><span>FrameSeeker</span><em>LOCAL</em></div>
        <div className="runtime">
          <span className={`statusDot ${health?.ok && health.database.online ? "online" : ""}`} />
          {health?.ok
            ? `Qdrant ${health.database.stored_points} 张 · ${health.device}`
            : "等待本地服务"}
        </div>
      </header>

      <section className="hero">
        <div>
          <p className="eyebrow">多模态媒体检索工作台</p>
          <h1>找到你脑海里的<br /><span>那一帧。</span></h1>
          <p className="lead">SigLIP 2 理解内容，DINOv3 捕捉构图，pHash 识别重复。Qdrant 会持久保存已计算的向量，同一素材无需反复推理。</p>
        </div>
        <div className="modelRail">
          <div><small>SEMANTIC</small><strong>SigLIP 2 Giant</strong><span>1536 维 · 1B</span></div>
          <div><small>VISUAL</small><strong>DINOv3 H+</strong><span>1280 维 · 840M</span></div>
          <div><small>DATABASE</small><strong>Qdrant</strong><span>双命名向量</span></div>
        </div>
      </section>

      <section className="workspace">
        <aside className="controls panel">
          <div className="step"><span>01</span><h2>加入素材</h2></div>
          <label className="dropzone">
            <input type="file" accept="image/*" multiple onChange={(event) => chooseFiles(event.target.files)} />
            <span className="dropIcon">＋</span>
            <strong>{files.length ? `已选择 ${files.length} 张图片` : "选择图片"}</strong>
            <small>JPG · PNG · WEBP · 最多 200 张</small>
          </label>

          <div className="step second"><span>02</span><h2>设置检索</h2></div>
          <div className="modeSwitch">
            <button className={mode === "text" ? "active" : ""} onClick={() => changeMode("text")}>文字搜图</button>
            <button className={mode === "image" ? "active" : ""} onClick={() => changeMode("image")}>以图搜图</button>
          </div>

          {mode === "text" ? (
            <label className="fieldLabel">描述想找的画面
              <textarea value={query} onChange={(event) => setQuery(event.target.value)} placeholder="例如：紫色灯光下的人物特写" />
            </label>
          ) : (
            <div className="referenceBox">
              <span>参考图片</span>
              <strong>{files[reference]?.name || "请从右侧素材中选择"}</strong>
              <small>点击右侧图片切换参考图，再点击开始检索</small>
            </div>
          )}

          {mode === "image" && (
            <div className="weightControl">
              <div><span>语义 {semanticWeight}%</span><span>构图 {100 - semanticWeight}%</span></div>
              <input type="range" min="0" max="100" value={semanticWeight} onChange={(event) => setSemanticWeight(Number(event.target.value))} />
            </div>
          )}

          <button className="searchButton" disabled={busy || !files.length} onClick={runSearch}>
            {busy ? <><span className="spinner" />正在分析画面</> : <>开始检索<span>→</span></>}
          </button>
          {error && <p className="error">{error}</p>}
        </aside>

        <section className="results panel">
          <div className="resultsHeader">
            <div><span className="stepNumber">03</span><h2>检索结果</h2></div>
            <p>{results.length && cacheStats
              ? `${results.length} 个结果 · ${elapsed} ms · 缓存命中 ${cacheStats.hits} · 新计算 ${cacheStats.misses}`
              : "按相似度降序排列"}</p>
          </div>

          {!files.length ? (
            <div className="emptyState"><span>◇</span><h3>等待加入素材</h3><p>选择图片后，缩略图和检索结果会出现在这里。</p></div>
          ) : !results.length ? (
            <div className="assetPicker">
              <p>{mode === "image" ? "点击一张图片设为参考图，绿框表示当前选择" : "素材已就绪，开始检索查看排序"}</p>
              <div className="previewGrid">
                {files.map((file, index) => (
                  <button
                    key={`${file.name}-${index}`}
                    type="button"
                    aria-pressed={reference === index && mode === "image"}
                    className={reference === index && mode === "image" ? "selected" : ""}
                    onClick={() => setReference(index)}
                  >
                    <img src={previews[index]} alt={file.name} />
                    {reference === index && mode === "image" && <i className="referenceBadge">参考图 ✓</i>}
                    <span>{file.name}</span>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="resultGrid">
              {results.map((item) => (
                <article
                  className={`resultCard ${mode === "image" ? "selectable" : ""} ${mode === "image" && reference === item.index ? "referenceSelected" : ""}`}
                  key={`${item.filename}-${item.index}`}
                  role={mode === "image" ? "button" : undefined}
                  tabIndex={mode === "image" ? 0 : undefined}
                  aria-label={mode === "image" ? `将 ${item.filename} 设为参考图` : undefined}
                  onClick={() => mode === "image" && setReference(item.index)}
                  onKeyDown={(event) => {
                    if (mode === "image" && (event.key === "Enter" || event.key === " ")) {
                      event.preventDefault();
                      setReference(item.index);
                    }
                  }}
                >
                  <div className="imageWrap">
                    <img src={previews[item.index]} alt={item.filename} />
                    <span className="rank">#{String(item.rank).padStart(2, "0")}</span>
                    {mode === "image" && reference === item.index && <span className="resultReference">新参考图 ✓</span>}
                    {item.phash_distance === 0 && mode === "image" && <span className="duplicate">重复</span>}
                    <span className={`cacheTag ${item.cache_hit ? "hit" : "new"}`}>{item.cache_hit ? "已复用" : "新入库"}</span>
                  </div>
                  <div className="cardBody">
                    <strong title={item.filename}>{item.filename}</strong>
                    <small>{item.width} × {item.height}</small>
                    <div className="scoreRow"><span>综合相似度</span><b>{(item.score * 100).toFixed(1)}%</b></div>
                    <div className="scoreTrack"><i style={{ width: `${Math.max(0, item.score * 100)}%` }} /></div>
                    <div className="subscores">
                      <span>语义 <b>{(item.semantic_score * 100).toFixed(1)}</b></span>
                      {item.visual_score !== null && <span>构图 <b>{(item.visual_score * 100).toFixed(1)}</b></span>}
                      {item.phash_distance !== null && <span>哈希 <b>{item.phash_distance}</b></span>}
                    </div>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>
      </section>

      <footer><span>FRAMESEEKER / LOCAL VISION LAB</span><span>向量与推理均保存在本机</span></footer>
    </main>
  );
}
