"""Generate an offline timeline viewer for one recorded Piper X episode."""
import argparse
import json
import math
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIEWS = ("left_wrist", "right_wrist", "overview")
SIDES = ("left", "right")
FIELDS = ("j1", "j2", "j3", "j4", "j5", "j6", "gripper_width")


def lines(path):
    with path.open(encoding="utf-8-sig") as file:
        for number, line in enumerate(file, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{number}: invalid JSON") from exc


def build(episode, output):
    metadata = json.loads((episode / "metadata.json").read_text(encoding="utf-8-sig"))
    start = metadata.get("start_host_monotonic_ns")
    end = metadata.get("stop_request_host_monotonic_ns")
    if start is None or end is None or end <= start:
        raise ValueError("Episode has no valid recording time range")
    duration = (end - start) / 1e9
    data = {"duration": duration, "metadata": metadata, "views": {}, "signals": {}}
    for view in VIEWS:
        folder = episode / "camera" / view
        index = folder / "frames.jsonl"
        rows = []
        if index.exists():
            for row in lines(index):
                if row.get("stream") != "rgb":
                    continue
                name = row["file"]
                if Path(name).name != name:
                    raise ValueError(f"Invalid image filename: {name}")
                path = folder / name
                if not path.is_file():
                    raise FileNotFoundError(path)
                rows.append([(row["host_monotonic_ns"] - start) / 1e9,
                             os.path.relpath(path, output.parent).replace(os.sep, "/"),
                             row["frame_number"]])
        data["views"][view] = sorted(rows, key=lambda item: item[0])
    for side in SIDES:
        path = episode / "can" / side / "signals.jsonl"
        series = {field: {"state": [], "action": []} for field in FIELDS}
        if path.exists():
            for row in lines(path):
                if not row.get("valid") or row.get("kind") not in ("state", "action"):
                    continue
                t = (row["host_monotonic_ns"] - start) / 1e9
                for field, value in zip(row["fields"], row["values"]):
                    if field in series and isinstance(value, (int, float)) and math.isfinite(value):
                        series[field][row["kind"]].append([t, value])
        for field in series.values():
            for values in field.values():
                values.sort(key=lambda item: item[0])
        data["signals"][side] = series
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", r"\u003c")
    html = TEMPLATE.replace("__DATA__", payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(html, encoding="utf-8")
    temporary.replace(output)


TEMPLATE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Piper X 轨迹回看</title>
<style>
body{font:15px "Microsoft YaHei",sans-serif;background:#f3f5f8;color:#182233;margin:0}
main{max-width:1500px;margin:auto;padding:20px} h1{font-size:22px;margin:0 0 8px}
.meta,.hint{color:#526077;font-size:13px;line-height:1.6}
.controls{display:flex;align-items:center;gap:12px;flex-wrap:wrap;background:white;padding:12px;border-radius:8px;margin:12px 0}
button,select{font:inherit;padding:7px 12px} input[type=range]{flex:1;min-width:220px}
.cameras{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.card,.chart{background:white;border-radius:8px;padding:10px;min-width:0}
.card h2{font-size:15px;margin:0 0 6px}.image{position:relative;aspect-ratio:4/3;background:#202938}
.image img{width:100%;height:100%;object-fit:contain}.missing{display:none;position:absolute;inset:0;place-items:center;color:white;background:#273142}
.info{font-size:13px;color:#526077;margin-top:5px}.chart{margin-top:12px}
canvas{width:100%;height:320px;display:block;cursor:crosshair}
.legend{display:flex;gap:18px;flex-wrap:wrap}.state{color:#0073c9}.action{color:#dc5c12}
@media(max-width:800px){.cameras{grid-template-columns:1fr}main{padding:10px}}
</style></head><body><main>
<h1>Piper X 轨迹回看</h1>
<div class="meta" id="meta"></div>
<div class="hint">时间基于数据到达电脑的单调时钟。画面选择当前时间以前最近一帧，括号内为距离当前时间的间隔。超过 150 ms 显示无新画面。状态与目标间隔超过 200 ms 时曲线断开；这是显示规则，动作保持与同步精度仍需验证。</div>
<div class="controls"><button id="play">播放</button><button id="back">−1 帧</button><button id="next">+1 帧</button>
<input id="seek" type="range" min="0" step="0.001" value="0"><strong id="time"></strong><label>速度 <select id="speed"><option value="0.5">0.5×</option><option value="1" selected>1×</option><option value="2">2×</option></select></label></div>
<div class="cameras" id="cameras"></div>
<div class="chart"><div class="controls"><label>机械臂 <select id="side"><option value="left">左侧</option><option value="right">右侧</option></select></label>
<label>关节 / 夹爪 <select id="field"><option value="j1">关节 1</option><option value="j2">关节 2</option><option value="j3">关节 3</option><option value="j4">关节 4</option><option value="j5">关节 5</option><option value="j6">关节 6</option><option value="gripper_width">夹爪宽度</option></select></label>
<span class="state">● 从臂实际状态</span><span class="action">● 主臂目标</span></div>
<canvas id="plot"></canvas><div class="info" id="values"></div>
<div class="hint">点击曲线跳转时间。数值显示当前时间之前最近一条有效报文及其年龄；超过 200 ms 会标记为陈旧。空档不代表已经确认的保持行为。</div></div>
</main><script>
"use strict";
const data=__DATA__;
const names={left_wrist:"左腕",right_wrist:"右腕",overview:"全景"};
const seek=document.getElementById("seek"), timeLabel=document.getElementById("time");
const side=document.getElementById("side"), field=document.getElementById("field"), canvas=document.getElementById("plot");
const ctx=canvas.getContext("2d");
const usabilityText={usable:"可用",unusable:"不可用",unreviewed:"待复核"};
document.getElementById("meta").textContent="任务："+data.metadata.task+"　活动臂："+(data.metadata.active_arms.join("、")||"无")+"　轨迹："+data.metadata.episode_id+"　采集标注："+(usabilityText[data.metadata.trajectory_usability]||"待复核");
seek.max=data.duration;
const cameras={};
for(const [id,label] of Object.entries(names)){
  const card=document.createElement("section");card.className="card";
  const heading=document.createElement("h2");heading.textContent=label;
  const frame=document.createElement("div");frame.className="image";
  const img=document.createElement("img");img.alt=label;
  const missing=document.createElement("div");missing.className="missing";missing.textContent="当前无新画面";
  const info=document.createElement("div");info.className="info";
  frame.append(img,missing);card.append(heading,frame,info);document.getElementById("cameras").append(card);
  cameras[id]={img,missing,info,last:null};
}
function atOrBefore(array,t){
  let lo=0,hi=array.length;
  while(lo<hi){const mid=(lo+hi)>>1;if(array[mid][0]<=t)lo=mid+1;else hi=mid;}
  return lo?array[lo-1]:null;
}
function renderCameras(t){
  for(const id of Object.keys(cameras)){
    const item=cameras[id],row=atOrBefore(data.views[id],t),age=row?t-row[0]:Infinity;
    item.missing.style.display=age>0.15?"grid":"none";
    if(row && item.last!==row[1]){item.img.src=row[1];item.last=row[1];}
    item.info.textContent=row?"帧 "+row[2]+" · 到达时间 "+row[0].toFixed(3)+" s · 相差 "+(age*1000).toFixed(0)+" ms":"尚无图像";
  }
}
function draw(t){
  const rect=canvas.getBoundingClientRect(), dpr=window.devicePixelRatio||1;
  canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);
  ctx.setTransform(dpr,0,0,dpr,0,0);const w=rect.width,h=rect.height;
  const left=66,right=18,top=20,bottom=38,pw=w-left-right,ph=h-top-bottom;
  const series=data.signals[side.value][field.value],all=series.state.concat(series.action);
  ctx.clearRect(0,0,w,h);ctx.fillStyle="#526077";ctx.font="12px sans-serif";
  if(!all.length){ctx.fillText("该侧没有此字段的有效数据",left,top+15);return;}
  let min=Infinity,max=-Infinity;
  for(const point of all){min=Math.min(min,point[1]);max=Math.max(max,point[1]);}
  const pad=Math.max((max-min)*0.08,field.value==="gripper_width"?0.001:0.01);min-=pad;max+=pad;
  const x=v=>left+v/data.duration*pw,y=v=>top+(max-v)/(max-min)*ph;
  ctx.strokeStyle="#dce2eb";ctx.lineWidth=1;
  for(let i=0;i<=4;i++){let yy=top+i*ph/4;ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(w-right,yy);ctx.stroke();ctx.fillText((max-(max-min)*i/4).toFixed(3),4,yy+4);}
  for(let i=0;i<=5;i++){let xx=left+i*pw/5;ctx.fillText((data.duration*i/5).toFixed(1),xx-8,h-12);}
  ctx.fillText(field.value==="gripper_width"?"m":"rad",8,12);ctx.fillText("秒",w-25,h-12);
  for(const [kind,color] of [["state","#0073c9"],["action","#dc5c12"]]){
    ctx.strokeStyle=color;ctx.lineWidth=kind==="state"?1.4:1.6;ctx.beginPath();let previous=null;
    for(const point of series[kind]){
      if(point[0]<0||point[0]>data.duration)continue;
      if(!previous || point[0]-previous[0]>0.2)ctx.moveTo(x(point[0]),y(point[1]));else ctx.lineTo(x(point[0]),y(point[1]));
      previous=point;
    }
    ctx.stroke();
  }
  ctx.strokeStyle="#4b5262";ctx.setLineDash([4,3]);ctx.beginPath();ctx.moveTo(x(t),top);ctx.lineTo(x(t),h-bottom);ctx.stroke();ctx.setLineDash([]);
  canvas.dataset.left=left;canvas.dataset.width=pw;
}
function showValue(t){
 const s=data.signals[side.value][field.value],unit=field.value==="gripper_width"?"m":"rad";
 function describe(kind,label){const p=atOrBefore(s[kind],t);if(!p)return label+"：无先前有效报文";
  const age=t-p[0];return label+"："+p[1].toFixed(4)+" "+unit+"（"+(age*1000).toFixed(0)+" ms 前"+(age>0.2?"，陈旧":"")+"）";}
 document.getElementById("values").textContent=describe("state","实际")+"　　"+describe("action","目标");
}
let playing=false,last=0;
function update(){const t=Number(seek.value);timeLabel.textContent=t.toFixed(3)+" / "+data.duration.toFixed(3)+" s";renderCameras(t);draw(t);showValue(t);}
function tick(now){if(!playing)return;if(last){seek.value=Math.min(data.duration,Number(seek.value)+(now-last)/1000*Number(document.getElementById("speed").value));update();}
 last=now;if(Number(seek.value)>=data.duration){playing=false;document.getElementById("play").textContent="播放";return;}requestAnimationFrame(tick);}
document.getElementById("play").onclick=()=>{playing=!playing;document.getElementById("play").textContent=playing?"暂停":"播放";if(playing){if(Number(seek.value)>=data.duration)seek.value=0;last=0;requestAnimationFrame(tick);}};
document.getElementById("back").onclick=()=>{seek.value=Math.max(0,Number(seek.value)-1/30);update();};
document.getElementById("next").onclick=()=>{seek.value=Math.min(data.duration,Number(seek.value)+1/30);update();};
seek.oninput=update;side.onchange=update;field.onchange=update;window.onresize=update;
canvas.onclick=e=>{const r=canvas.getBoundingClientRect();seek.value=Math.max(0,Math.min(data.duration,(e.clientX-r.left-Number(canvas.dataset.left))/Number(canvas.dataset.width)*data.duration));update();};
update();
</script></body></html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path, help="Path to a recorded episode directory")
    parser.add_argument("--output", type=Path, help="HTML output path")
    args = parser.parse_args()
    episode = args.episode.resolve()
    metadata = json.loads((episode / "metadata.json").read_text(encoding="utf-8-sig"))
    output = (args.output or ROOT / "data" / "reviews" / (metadata["episode_id"] + ".html")).resolve()
    if output.is_relative_to(episode):
        parser.error("Output must be outside the original episode directory")
    build(episode, output)
    print(f"Review: {output}")


if __name__ == "__main__":
    main()
