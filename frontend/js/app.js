import * as THREE from 'three';
import * as TWEEN from '@tweenjs/tween.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';

const container=document.getElementById('view3d');
const loaderEl=document.getElementById('loader');
const W=()=>Math.max(container.clientWidth,1);
const H=()=>Math.max(container.clientHeight,1);

const scene=new THREE.Scene();
scene.background=new THREE.Color(0x050806);
scene.fog=new THREE.FogExp2(0x050806,.018);

const camera=new THREE.PerspectiveCamera(43,W()/H(),.3,220);
const camTarget=new THREE.Vector3(0,1.2,0);
const defaultTarget=new THREE.Vector3(0,1.2,0);
const ca={theta:.72,phi:1.05};
let cd=52,defaultDist=52,defaultPhi=1.05,defaultTheta=.72;
camera.position.set(28,24,38);
camera.lookAt(camTarget);

const renderer=new THREE.WebGLRenderer({antialias:true,powerPreference:'high-performance'});
renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,1.75));
renderer.setSize(W(),H());
renderer.outputColorSpace=THREE.SRGBColorSpace;
renderer.toneMapping=THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure=.78;
renderer.shadowMap.enabled=true;
renderer.shadowMap.type=THREE.PCFSoftShadowMap;
container.appendChild(renderer.domElement);

const labelRenderer=new CSS2DRenderer();
labelRenderer.setSize(W(),H());
labelRenderer.domElement.style.position='absolute';
labelRenderer.domElement.style.inset='0';
labelRenderer.domElement.style.pointerEvents='none';
container.appendChild(labelRenderer.domElement);

const composer=new EffectComposer(renderer);
composer.addPass(new RenderPass(scene,camera));
const bloomPass=new UnrealBloomPass(new THREE.Vector2(W(),H()),.56,.22,.88);
composer.addPass(bloomPass);

scene.add(new THREE.HemisphereLight(0x0a1628,0x05080a,.45));
const sun=new THREE.DirectionalLight(0xaaddff,.4);
sun.position.set(42,58,32);
sun.castShadow=true;
sun.shadow.mapSize.set(1024,1024);
sun.shadow.camera.near=1;
sun.shadow.camera.far=150;
sun.shadow.camera.left=-55;
sun.shadow.camera.right=55;
sun.shadow.camera.top=55;
sun.shadow.camera.bottom=-55;
scene.add(sun);
const alertLight=new THREE.PointLight(0xff5c65,18,32);
alertLight.position.set(-12,8,-7);
scene.add(alertLight);
const fieldLight=new THREE.PointLight(0x0088ff,10,38);
fieldLight.position.set(12,10,8);
scene.add(fieldLight);

const floorMat=new THREE.MeshStandardMaterial({color:0x030608,roughness:.88,metalness:.08,emissive:0x001019,emissiveIntensity:.18});
const floor=new THREE.Mesh(new THREE.PlaneGeometry(130,130),floorMat);
floor.rotation.x=-Math.PI/2;
floor.receiveShadow=true;
scene.add(floor);

const polar=new THREE.PolarGridHelper(32,48,24,96,0xffb74a,0x24423c);
polar.position.y=.03;
polar.material.transparent=true;
polar.material.opacity=.26;
scene.add(polar);

const pGeo=new THREE.BufferGeometry();
const pCount=480;
const pArr=new Float32Array(pCount*3);
for(let i=0;i<pCount;i++){
  pArr[i*3]=(Math.random()-.5)*78;
  pArr[i*3+1]=Math.random()*28+1;
  pArr[i*3+2]=(Math.random()-.5)*78;
}
pGeo.setAttribute('position',new THREE.BufferAttribute(pArr,3));
const particles=new THREE.Points(pGeo,new THREE.PointsMaterial({color:0x62f3dd,size:.045,transparent:true,opacity:.46,blending:THREE.AdditiveBlending,depthWrite:false}));
scene.add(particles);

function createZone(x,z,r,color,label){
  const group=new THREE.Group();
  const ring=new THREE.Mesh(new THREE.TorusGeometry(r,.035,12,96),new THREE.MeshBasicMaterial({color,transparent:true,opacity:.48,blending:THREE.AdditiveBlending}));
  ring.rotation.x=-Math.PI/2;
  group.add(ring);
  const post=new THREE.Mesh(new THREE.CylinderGeometry(.035,.035,2.2,8),new THREE.MeshBasicMaterial({color,transparent:true,opacity:.35}));
  post.position.y=1.1;
  group.add(post);
  const div=document.createElement('div');
  div.className='label3d';
  const labelHex='#'+color.toString(16).padStart(6,'0');
  const rC=(color>>16)&255;
  const gC=(color>>8)&255;
  const bC=color&255;
  div.style.setProperty('--label-color',labelHex);
  div.style.setProperty('--label-bg',`rgba(${rC}, ${gC}, ${bC}, 0.18)`);
  div.textContent=label;
  const tag=new CSS2DObject(div);
  tag.position.set(0,2.55,0);
  group.add(tag);
  group.position.set(x,.04,z);
  group.userData={spin:.0015+Math.random()*.001};
  scene.add(group);
  return group;
}
const zones=[
  createZone(-9,-5,2.2,0xffb74a,'装配区'),
  createZone(7,4,2.6,0x62f3dd,'物流通道'),
  createZone(2,-8,1.8,0xff5c65,'高危点')
];

const dataFlows=[],scanWaves=[],transientEffects=[];
function createDataFlow(points,color,offset=0){
  const curve=new THREE.CatmullRomCurve3(points);
  const lineGeo=new THREE.BufferGeometry().setFromPoints(curve.getPoints(90));
  const lineMat=new THREE.LineBasicMaterial({color,transparent:true,opacity:.22,blending:THREE.AdditiveBlending,depthWrite:false});
  const line=new THREE.Line(lineGeo,lineMat);
  line.position.y=.08;
  line.renderOrder=1;
  scene.add(line);
  const dot=new THREE.Mesh(new THREE.SphereGeometry(.12,12,8),new THREE.MeshBasicMaterial({color,transparent:true,opacity:.9,blending:THREE.AdditiveBlending,depthWrite:false}));
  dot.renderOrder=5;
  scene.add(dot);
  dataFlows.push({curve,dot,t:offset,speed:.045+Math.random()*.018});
}
createDataFlow([new THREE.Vector3(-16,0,-9),new THREE.Vector3(-6,0,-4),new THREE.Vector3(0,0,0),new THREE.Vector3(8,0,4),new THREE.Vector3(15,0,7)],0x62f3dd,.1);
createDataFlow([new THREE.Vector3(14,0,-8),new THREE.Vector3(6,0,-5),new THREE.Vector3(2,0,-1),new THREE.Vector3(-7,0,4),new THREE.Vector3(-15,0,9)],0x0088ff,.45);
createDataFlow([new THREE.Vector3(-13,0,6),new THREE.Vector3(-5,0,3),new THREE.Vector3(2,0,-2),new THREE.Vector3(9,0,-5)],0xffb74a,.72);

for(let i=0;i<3;i++){
  const ring=new THREE.Mesh(new THREE.TorusGeometry(1,.018,8,128),new THREE.MeshBasicMaterial({color:0x62f3dd,transparent:true,opacity:.18,blending:THREE.AdditiveBlending,depthWrite:false}));
  ring.rotation.x=-Math.PI/2;
  ring.position.y=.075;
  ring.userData={phase:i/3};
  scene.add(ring);
  scanWaves.push(ring);
}

const manager=new THREE.LoadingManager();
manager.onLoad=()=>{if(loaderEl){loaderEl.style.opacity='0';loaderEl.style.pointerEvents='none';setTimeout(()=>loaderEl.remove(),350)}};
manager.onError=url=>console.warn('[3D] 资源加载失败:',url);
const gltfLoader=new GLTFLoader(manager);
const workerModels={};

gltfLoader.load('construction_worker_in_safety_gear.glb',gltf=>{
  workerModels.safety=prepareWorker(gltf.scene);
  console.log('[3D] 安全装备工人已加载');
});
gltfLoader.load('construction_worker_in_high-visibility_vest.glb',gltf=>{
  workerModels.vest=prepareWorker(gltf.scene);
  console.log('[3D] 反光背心工人已加载');
});

function prepareWorker(root){
  root.traverse(c=>{
    if(c.isMesh){
      c.castShadow=true;
      c.receiveShadow=true;
      c.frustumCulled=true;
    }
  });
  return root;
}

gltfLoader.load('low_poly_industrial_zone.glb',gltf=>{
  const root=gltf.scene;
  root.scale.set(.64,.64,.64);
  root.position.set(0,0,0);
  root.traverse(c=>{
    const name=(c.name||'').toLowerCase();
    if(name.includes('tree')||name.includes('foliage')||name.includes('leaf')||name.includes('leaves')||name.includes('grass')||name.includes('bush')){
      c.visible=false;
      return;
    }
    if(!c.isMesh)return;
    c.castShadow=false;
    c.receiveShadow=false;
    c.material=new THREE.MeshStandardMaterial({
      color:0x03070d,
      emissive:0x001a33,
      emissiveIntensity:.18,
      roughness:.72,
      metalness:.18,
      transparent:true,
      opacity:.38,
      depthWrite:false
    });
    if(c.geometry){
      const edges=new THREE.LineSegments(
        new THREE.EdgesGeometry(c.geometry,35),
        new THREE.LineBasicMaterial({
          color:0x00a6ff,
          transparent:true,
          opacity:.48,
          blending:THREE.AdditiveBlending,
          depthWrite:false
        })
      );
      edges.renderOrder=2;
      c.add(edges);
    }
  });
  scene.add(root);
  console.log('[3D] 工厂模型已加载');
},undefined,()=>console.log('[3D] 工厂模型加载失败'));

function createFallbackHuman(color=0x62f3dd,violations=[]){
  const group=new THREE.Group();
  const bodyMat=new THREE.MeshStandardMaterial({
    color:violations.includes('no_vest')?0x6a4520:0x10292d,
    emissive:color,
    emissiveIntensity:.035,
    roughness:1,
    metalness:0,
    transparent:true,
    opacity:.58,
    blending:THREE.NormalBlending,
    depthTest:true,
    depthWrite:true
  });
  bodyMat.userData={tracksState:true,bodyMaterial:true,bodyColor:bodyMat.color.getHex()};
  const headMat=bodyMat.clone();
  headMat.color.setHex(violations.includes('no_helmet')?0x7a1f2c:0x173f46);
  headMat.userData={tracksState:true,bodyMaterial:true,bodyColor:headMat.color.getHex()};
  const body=new THREE.Mesh(new THREE.CapsuleGeometry(.38,1.35,4,10),bodyMat);
  body.position.y=1.35;
  const head=new THREE.Mesh(new THREE.SphereGeometry(.3,16,12),headMat);
  head.position.y=2.35;
  body.castShadow=head.castShadow=false;
  body.receiveShadow=head.receiveShadow=false;
  group.add(body,head);
  return group;
}

const humans=[],labelGroups=[],groundRings=[];
const trackedHumans={};
const HUMAN_STALE_AFTER=30000;
const HUMAN_TRACK_TTL=120000;
const HUMAN_FADE_DURATION=4500;
const HUMAN_RING_TTL=30000;
const HUMAN_BASE_COLOR=new THREE.Color(0x4b6f6b);
const HUMAN_HEAD_ALERT=new THREE.Color(0xff3048);
const HUMAN_CHEST_ALERT=new THREE.Color(0xffb74a);
const HUMAN_DISPLAY_AREA={
  // 中间无遮挡空地。若位置略偏，只需要调 center/width/depth。
  center:new THREE.Vector3(0,0,-.35),
  width:7.2,
  depth:3.2,
  xInvert:false,
  zInvert:false,
  rotationY:.72
};

function dimColor(color,factor=.16){
  return new THREE.Color(color).multiplyScalar(factor);
}

function normalizeBBox(b={}){
  const x=Number(b.x??b.left??b.x1??0);
  const y=Number(b.y??b.top??b.y1??0);
  const right=Number(b.right??b.x2??NaN);
  const bottom=Number(b.bottom??b.y2??NaN);
  let width=Number(b.width??b.w??(Number.isFinite(right)?right-x:80));
  let height=Number(b.height??b.h??(Number.isFinite(bottom)?bottom-y:160));
  if(width<0)width=Math.abs(width);
  if(height<0)height=Math.abs(height);
  return {x,y,width:Math.max(width,1),height:Math.max(height,1)};
}

function trackKeyFor(ev,bbox,data={}){
  const raw=ev.targetId;
  if(raw!==undefined&&raw!==null&&String(raw).trim()!==''&&String(raw)!=='0')return `tid:${String(raw)}`;
  const cam=String(ev.cameraId||data.cameraId||'CAM-01');
  const cx=bbox.x+bbox.width/2;
  const foot=bbox.y+bbox.height;
  return `pos:${cam}:${Math.round(cx/72)}:${Math.round(foot/72)}`;
}

function displayPositionFor(tid,bbox,data={}){
  const box=normalizeBBox(bbox);
  const cx=box.x+box.width/2;
  const foot=box.y+box.height;
  let nx=THREE.MathUtils.clamp(cx/MAP_W,0,1);
  let nz=THREE.MathUtils.clamp(foot/MAP_H,0,1);
  if(HUMAN_DISPLAY_AREA.xInvert)nx=1-nx;
  if(HUMAN_DISPLAY_AREA.zInvert)nz=1-nz;
  const halfW=(HUMAN_DISPLAY_AREA.width??7.2)/2;
  const halfD=(HUMAN_DISPLAY_AREA.depth??3.2)/2;
  const pos=new THREE.Vector3(
    HUMAN_DISPLAY_AREA.center.x+THREE.MathUtils.clamp((nx-.5)*HUMAN_DISPLAY_AREA.width,-halfW,halfW),
    0,
    HUMAN_DISPLAY_AREA.center.z+THREE.MathUtils.clamp((nz-.5)*HUMAN_DISPLAY_AREA.depth,-halfD,halfD)
  );
  pos.y=0;
  return {position:pos,rotationY:HUMAN_DISPLAY_AREA.rotationY??0,nx,nz};
}

function smooth01(edge0,edge1,x){
  const t=THREE.MathUtils.clamp((x-edge0)/(edge1-edge0),0,1);
  return t*t*(3-2*t);
}

function semanticColorAt(yNorm,violations,stateColor){
  const color=HUMAN_BASE_COLOR.clone();
  const stateTint=dimColor(stateColor,.52);
  color.lerp(stateTint,.2);
  if(violations.includes('intrusion')){
    color.lerp(new THREE.Color(0xff5c65),.16);
  }
  if(violations.includes('proximity')){
    color.lerp(new THREE.Color(0xff7b3d),.18);
  }
  if(violations.includes('no_vest')){
    const chestMask=smooth01(.36,.48,yNorm)*(1-smooth01(.64,.76,yNorm));
    color.lerp(HUMAN_CHEST_ALERT,chestMask*.95);
  }
  if(violations.includes('no_helmet')){
    const headMask=smooth01(.72,.86,yNorm);
    color.lerp(HUMAN_HEAD_ALERT,headMask);
  }
  return color;
}

function applyHumanGradient(mesh,humanHeight,violations,stateColor){
  const pos=mesh.geometry?.attributes?.position;
  if(!pos||!pos.count)return;
  mesh.geometry=mesh.geometry.clone();
  const clonedPos=mesh.geometry.attributes.position;
  mesh.updateWorldMatrix(true,false);
  const colors=new Float32Array(clonedPos.count*3);
  const tmp=new THREE.Vector3();
  for(let i=0;i<clonedPos.count;i++){
    tmp.set(clonedPos.getX(i),clonedPos.getY(i),clonedPos.getZ(i));
    mesh.localToWorld(tmp);
    const yNorm=THREE.MathUtils.clamp(tmp.y/Math.max(humanHeight,.001),0,1);
    const c=semanticColorAt(yNorm,violations,stateColor);
    colors[i*3]=c.r;
    colors[i*3+1]=c.g;
    colors[i*3+2]=c.b;
  }
  mesh.geometry.setAttribute('color',new THREE.BufferAttribute(colors,3));
}

function createSparsePointGeometry(geometry,step=6,mesh=null,humanHeight=2.85,violations=[],stateColor=0x62f3dd){
  const pos=geometry?.attributes?.position;
  if(!pos||!pos.count)return null;
  const safeStep=Math.max(2,Math.floor(step));
  const count=Math.ceil(pos.count/safeStep);
  const arr=new Float32Array(count*3);
  const colors=new Float32Array(count*3);
  const tmp=new THREE.Vector3();
  let j=0;
  for(let i=0;i<pos.count;i+=safeStep){
    arr[j*3]=pos.getX(i);
    arr[j*3+1]=pos.getY(i);
    arr[j*3+2]=pos.getZ(i);
    tmp.set(arr[j*3],arr[j*3+1],arr[j*3+2]);
    if(mesh)mesh.localToWorld(tmp);
    const yNorm=THREE.MathUtils.clamp(tmp.y/Math.max(humanHeight,.001),0,1);
    const c=semanticColorAt(yNorm,violations,stateColor);
    colors[j*3]=c.r;
    colors[j*3+1]=c.g;
    colors[j*3+2]=c.b;
    j++;
  }
  const sparse=new THREE.BufferGeometry();
  sparse.setAttribute('position',new THREE.BufferAttribute(arr,3));
  sparse.setAttribute('color',new THREE.BufferAttribute(colors,3));
  return sparse;
}

function createSignalLine(points,color,opacity=.7){
  const mat=new THREE.LineBasicMaterial({
    color,
    transparent:true,
    opacity,
    blending:THREE.AdditiveBlending,
    depthWrite:false
  });
  mat.userData={tracksState:true,signalMaterial:true,baseOpacity:opacity};
  const line=new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(points),mat);
  line.renderOrder=5;
  return line;
}

function addTargetSignature(group,height,stateColor,violations){
  const y0=.34,y1=height*.94,x=.78,z=.1,c=.22;
  const corners=[
    new THREE.Vector3(-x,y1,z),new THREE.Vector3(-x+c,y1,z),
    new THREE.Vector3(-x,y1,z),new THREE.Vector3(-x,y1-c,z),
    new THREE.Vector3(x,y1,z),new THREE.Vector3(x-c,y1,z),
    new THREE.Vector3(x,y1,z),new THREE.Vector3(x,y1-c,z),
    new THREE.Vector3(-x,y0,z),new THREE.Vector3(-x+c,y0,z),
    new THREE.Vector3(-x,y0,z),new THREE.Vector3(-x,y0+c,z),
    new THREE.Vector3(x,y0,z),new THREE.Vector3(x-c,y0,z),
    new THREE.Vector3(x,y0,z),new THREE.Vector3(x,y0+c,z)
  ];
  const lock=createSignalLine(corners,stateColor,.28);
  lock.userData.isTargetLock=true;
  lock.userData.pulseSpeed=.006;
  lock.userData.baseOpacity=.28;
  group.add(lock);

  const scanMat=new THREE.MeshBasicMaterial({
    color:stateColor,
    transparent:true,
    opacity:.055,
    side:THREE.DoubleSide,
    blending:THREE.AdditiveBlending,
    depthWrite:false
  });
  scanMat.userData={tracksState:true,signalMaterial:true,baseOpacity:.055};
  const scan=new THREE.Mesh(new THREE.RingGeometry(.22,.9,54,1),scanMat);
  scan.rotation.x=-Math.PI/2;
  scan.position.set(0,height*.38,0);
  scan.userData={isHumanScan:true,height,phase:Math.random(),baseOpacity:.1};
  scan.renderOrder=6;
  group.add(scan);

  const beaconColor=violations.length?stateColor:0x62f3dd;
  const beaconMat=new THREE.MeshBasicMaterial({
    color:beaconColor,
    transparent:true,
    opacity:.24,
    blending:THREE.AdditiveBlending,
    depthWrite:false
  });
  beaconMat.userData={tracksState:true,signalMaterial:true,baseOpacity:.24};
  const beacon=new THREE.Mesh(new THREE.ConeGeometry(.16,.34,3),beaconMat);
  beacon.position.y=height+0.42;
  beacon.rotation.z=Math.PI;
  beacon.userData={isRiskBeacon:true,pulseSpeed:.006,baseY:height+0.42};
  beacon.renderOrder=6;
  group.add(beacon);

  const sweepMat=new THREE.MeshBasicMaterial({
    color:stateColor,
    transparent:true,
    opacity:.09,
    side:THREE.DoubleSide,
    blending:THREE.AdditiveBlending,
    depthWrite:false
  });
  sweepMat.userData={tracksState:true,signalMaterial:true,baseOpacity:.09};
  const sweep=new THREE.Mesh(new THREE.RingGeometry(.62,1.22,42,1,0,Math.PI*.44),sweepMat);
  sweep.rotation.x=-Math.PI/2;
  sweep.position.y=.09;
  sweep.userData={isRadarSweep:true,rotSpeed:.025,baseOpacity:.09};
  sweep.renderOrder=4;
  group.add(sweep);
}

function createHuman(pos, violations, stateColor=0x62f3dd,rotationY=0){
  const group=new THREE.Group();
  const hasHelmet=!violations.includes('no_helmet');
  const hasVest=!violations.includes('no_vest');
  const src=(hasHelmet&&hasVest)?workerModels.safety:(workerModels.vest||workerModels.safety);

  if(src){
    const clone=src.clone(true);
    clone.scale.set(2.35,2.35,2.35);
    const box=new THREE.Box3().setFromObject(clone);
    clone.position.y=-box.min.y;
    const humanHeight=Math.max(box.max.y-box.min.y,3.2);
    clone.updateWorldMatrix(true,true);
    clone.traverse(c=>{
      if(!c.isMesh)return;
      c.castShadow=false;
      c.receiveShadow=false;
      c.frustumCulled=false;
      c.renderOrder=3;
      const bodyColor=new THREE.Color(0xe5f4ef);
      applyHumanGradient(c,humanHeight,violations,stateColor);
      const bodyMat=new THREE.MeshStandardMaterial({
        color:bodyColor,
        emissive:dimColor(stateColor,.18),
        emissiveIntensity:.018,
        roughness:1,
        metalness:0,
        vertexColors:true,
        transparent:true,
        opacity:.82,
        blending:THREE.NormalBlending,
        depthTest:true,
        depthWrite:true
      });
      bodyMat.userData={tracksState:true,bodyMaterial:true,bodyColor:bodyColor.getHex()};
      c.material=bodyMat;
      if(c.geometry){
        const pointGeometry=createSparsePointGeometry(c.geometry,violations.length?4:8,c,humanHeight,violations,stateColor);
        const pointsMat=new THREE.PointsMaterial({
          color:0xffffff,
          vertexColors:true,
          size:violations.length?.012:.01,
          transparent:true,
          opacity:violations.length?.28:.1,
          blending:THREE.NormalBlending,
          sizeAttenuation:true,
          depthTest:true,
          depthWrite:false
        });
        pointsMat.userData={tracksState:true,signalMaterial:true,baseOpacity:pointsMat.opacity};
        if(pointGeometry){
          const points=new THREE.Points(pointGeometry,pointsMat);
          points.renderOrder=4;
          c.add(points);
        }
      }
    });
    group.add(clone);
    addHumanTelemetryFrame(group,humanHeight,stateColor);
    addViolationHud(group,humanHeight,violations,stateColor);
    addTargetSignature(group,humanHeight,stateColor,violations);
  }else{
    const fallback=createFallbackHuman(stateColor,violations);
    group.add(fallback);
    addHumanTelemetryFrame(group,2.7,stateColor);
    addViolationHud(group,2.7,violations,stateColor);
    addTargetSignature(group,2.7,stateColor,violations);
  }

  const ringMat=new THREE.MeshBasicMaterial({color:stateColor,transparent:true,opacity:.24,blending:THREE.AdditiveBlending,depthWrite:false});
  ringMat.userData={tracksState:true};
  const ring=new THREE.Mesh(new THREE.TorusGeometry(.92,.028,16,64),ringMat);
  ring.rotation.x=-Math.PI/2;
  ring.position.y=.07;
  ring.userData={isBaseRing:true};
  group.add(ring);

  group.position.copy(pos);
  group.position.y=0;
  group.rotation.y=rotationY;
  scene.add(group);
  return group;
}

function recolorHumanSignal(group,color){
  group.traverse(o=>{
    const materials=o.material?Array.isArray(o.material)?o.material:[o.material]:[];
    for(const mat of materials){
      if(!mat.userData?.tracksState)continue;
      if(mat.userData.bodyMaterial){
        if(mat.emissive)mat.emissive.copy(dimColor(color,.18));
        if(mat.color)mat.color.setHex(mat.userData.bodyColor??0x10292d);
      }else{
        if(mat.emissive)mat.emissive.copy(dimColor(color,.2));
        if(mat.color)mat.color.setHex(color);
      }
    }
  });
}

function addHumanTelemetryFrame(group,height,stateColor){
  const yFoot=.28;
  const yHead=height*.92;
  const yShoulder=height*.66;
  const yChest=height*.56;
  const yHip=height*.42;
  const points=[
    new THREE.Vector3(0,yFoot,.04),new THREE.Vector3(0,yHead,.04),
    new THREE.Vector3(-.58,yShoulder,.04),new THREE.Vector3(.58,yShoulder,.04),
    new THREE.Vector3(-.42,yHip,.04),new THREE.Vector3(.42,yHip,.04),
    new THREE.Vector3(-.58,yShoulder,.04),new THREE.Vector3(-.42,yHip,.04),
    new THREE.Vector3(.58,yShoulder,.04),new THREE.Vector3(.42,yHip,.04),
    new THREE.Vector3(-.36,yChest,.04),new THREE.Vector3(.36,yChest,.04)
  ];
  const geo=new THREE.BufferGeometry().setFromPoints(points);
  const mat=new THREE.LineBasicMaterial({
    color:stateColor,
    transparent:true,
    opacity:.22,
    blending:THREE.NormalBlending,
    depthWrite:false
  });
  mat.userData={tracksState:true};
  const rig=new THREE.LineSegments(geo,mat);
  rig.renderOrder=4;
  group.add(rig);
}

function addViolationHud(group,height,violations,stateColor){
  if(violations.includes('no_helmet')){
    const helmetColor=0xff3048;
    const halo=new THREE.Mesh(new THREE.SphereGeometry(.42,24,14,0,Math.PI*2,0,Math.PI*.62),new THREE.MeshBasicMaterial({color:helmetColor,transparent:true,opacity:.12,blending:THREE.AdditiveBlending,depthWrite:false,side:THREE.DoubleSide}));
    halo.position.y=height*.89;
    halo.userData={isHUD:true,rotSpeed:.018,pulseSpeed:.12};
    halo.renderOrder=7;
    group.add(halo);
    const ring=new THREE.Mesh(new THREE.TorusGeometry(.55,.018,8,56),new THREE.MeshBasicMaterial({color:helmetColor,transparent:true,opacity:.38,blending:THREE.AdditiveBlending,depthWrite:false}));
    ring.position.y=height*.9;
    ring.rotation.x=Math.PI/2;
    ring.userData={isHUD:true,rotSpeed:.085,pulseSpeed:.09};
    ring.renderOrder=8;
    group.add(ring);
    const dot=new THREE.Mesh(new THREE.SphereGeometry(.06,16,12),new THREE.MeshBasicMaterial({color:helmetColor,transparent:true,opacity:.34,blending:THREE.AdditiveBlending,depthWrite:false}));
    dot.position.y=height*.98;
    dot.userData={isHUD:true,rotSpeed:0,pulseSpeed:.16};
    dot.renderOrder=8;
    group.add(dot);
  }
  if(violations.includes('no_vest')){
    const vestColor=0xffb74a;
    const plate=new THREE.Mesh(new THREE.BoxGeometry(1.14,.62,.08),new THREE.MeshBasicMaterial({color:vestColor,transparent:true,opacity:.16,blending:THREE.AdditiveBlending,depthWrite:false}));
    plate.position.y=height*.56;
    plate.userData={isHUD:true,rotSpeed:0,pulseSpeed:.08};
    plate.renderOrder=7;
    group.add(plate);
    const sweep=new THREE.Mesh(new THREE.BoxGeometry(1.2,.08,.1),new THREE.MeshBasicMaterial({color:vestColor,transparent:true,opacity:.34,blending:THREE.AdditiveBlending,depthWrite:false}));
    sweep.position.y=height*.46;
    sweep.userData={isChestSweep:true,height,baseY:height*.42,range:height*.24,pulseSpeed:.005};
    sweep.renderOrder=8;
    group.add(sweep);
    const edges=new THREE.EdgesGeometry(new THREE.BoxGeometry(1.1,.34,.74));
    const box=new THREE.LineSegments(edges,new THREE.LineBasicMaterial({color:vestColor,transparent:true,opacity:.36,blending:THREE.AdditiveBlending,depthWrite:false}));
    box.position.y=height*.56;
    box.userData={isHUD:true,rotSpeed:-.045,pulseSpeed:.07};
    box.renderOrder=8;
    group.add(box);
  }
  if(violations.includes('intrusion')){
    const dangerColor=0xff5c65;
    const zone=new THREE.Mesh(new THREE.RingGeometry(.78,1.42,64),new THREE.MeshBasicMaterial({color:dangerColor,transparent:true,opacity:.2,blending:THREE.AdditiveBlending,depthWrite:false,side:THREE.DoubleSide}));
    zone.rotation.x=-Math.PI/2;
    zone.position.y=.11;
    zone.userData={isIntrusionZone:true,baseOpacity:.2,rotSpeed:.018};
    zone.renderOrder=5;
    group.add(zone);
    for(let i=0;i<4;i++){
      const post=new THREE.Mesh(new THREE.CylinderGeometry(.018,.018,.8,8),new THREE.MeshBasicMaterial({color:dangerColor,transparent:true,opacity:.32,blending:THREE.AdditiveBlending,depthWrite:false}));
      const a=i*Math.PI/2+Math.PI/4;
      post.position.set(Math.cos(a)*1.1,.44,Math.sin(a)*1.1);
      post.userData={isHUD:true,rotSpeed:0,pulseSpeed:.1};
      post.renderOrder=6;
      group.add(post);
    }
  }
  if(violations.includes('proximity')){
    const riskColor=0xff7b3d;
    const geo=new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(.35,height*.52,.15),
      new THREE.Vector3(1.42,height*.52,.15)
    ]);
    const line=new THREE.Line(geo,new THREE.LineBasicMaterial({color:riskColor,transparent:true,opacity:.46,blending:THREE.AdditiveBlending,depthWrite:false}));
    line.userData={isProximityLine:true,baseOpacity:.46};
    line.renderOrder=8;
    group.add(line);
    const marker=new THREE.Mesh(new THREE.BoxGeometry(.22,.22,.22),new THREE.MeshBasicMaterial({color:riskColor,transparent:true,opacity:.35,blending:THREE.AdditiveBlending,depthWrite:false}));
    marker.position.set(1.55,height*.52,.15);
    marker.userData={isHUD:true,rotSpeed:.03,pulseSpeed:.13};
    marker.renderOrder=8;
    group.add(marker);
  }
  if(violations.includes('fire')){
    const fireColor=0xff3048;
    const heat=new THREE.Mesh(new THREE.CylinderGeometry(.18,.5,2.8,28,1,true),new THREE.MeshBasicMaterial({color:fireColor,transparent:true,opacity:.18,blending:THREE.AdditiveBlending,depthWrite:false,side:THREE.DoubleSide}));
    heat.position.set(-.95,1.4,.35);
    heat.userData={isHeatColumn:true,baseOpacity:.18};
    heat.renderOrder=7;
    group.add(heat);
    const ring=new THREE.Mesh(new THREE.RingGeometry(.28,.92,42),new THREE.MeshBasicMaterial({color:fireColor,transparent:true,opacity:.28,blending:THREE.AdditiveBlending,depthWrite:false,side:THREE.DoubleSide}));
    ring.rotation.x=-Math.PI/2;
    ring.position.set(-.95,.12,.35);
    ring.userData={isFirePulse:true,baseOpacity:.28};
    ring.renderOrder=7;
    group.add(ring);
  }
}

function createLabel(pos, violations, color=0x62f3dd, targetId='T-0000'){
  const texts=[];
  if(violations.includes('no_helmet'))texts.push('未戴安全帽');
  if(violations.includes('no_vest'))texts.push('未穿反光背心');
  if(violations.includes('fire'))texts.push('火焰');
  const div=document.createElement('div');
  div.className='label3d';
  const rC=(color>>16)&255;
  const gC=(color>>8)&255;
  const bC=color&255;
  div.style.setProperty('--label-color',`rgb(${rC}, ${gC}, ${bC})`);
  div.style.setProperty('--label-bg',`rgba(${rC}, ${gC}, ${bC}, 0.25)`);
  div.innerHTML=`<span class="target">TARGET ${escapeHtml(targetId)}</span><span class="reason">${escapeHtml(texts.join(' · ')||'人员')}</span>`;
  const label=new CSS2DObject(div);
  label.position.set(1.5,4.2,0);
  label.userData={created:Date.now(),lifetime:12000};
  const g=new THREE.Group();
  g.position.copy(pos);
  g.add(label);

  const lineGeo=new THREE.BufferGeometry().setFromPoints([
    new THREE.Vector3(0,.18,0),
    new THREE.Vector3(1.5,4.05,0)
  ]);
  const rF=rC/255;
  const gF=gC/255;
  const bF=bC/255;
  lineGeo.setAttribute('color',new THREE.Float32BufferAttribute([
    rF,gF,bF,
    rF*.05,gF*.05,bF*.05
  ],3));
  const lineMat=new THREE.LineBasicMaterial({
    vertexColors:true,
    transparent:true,
    blending:THREE.AdditiveBlending,
    opacity:.58,
    depthWrite:false
  });
  const tether=new THREE.Line(lineGeo,lineMat);
  tether.renderOrder=3;
  g.add(tether);

  scene.add(g);
  return g;
}

function createGroundRing(pos,color){
  const ring=new THREE.Mesh(new THREE.TorusGeometry(.56,.024,14,48),new THREE.MeshBasicMaterial({color,transparent:true,opacity:.28,blending:THREE.AdditiveBlending,depthWrite:false}));
  ring.rotation.x=-Math.PI/2;
  ring.position.copy(pos);
  ring.position.y=.08;
  ring.userData={created:Date.now(),lifetime:HUMAN_RING_TTL};
  scene.add(ring);
  return ring;
}

function createHumanTrail(from,to,color){
  if(!from||from.distanceTo(to)<.08)return;
  const points=[
    new THREE.Vector3(from.x,.12,from.z),
    new THREE.Vector3((from.x+to.x)/2,.14,(from.z+to.z)/2),
    new THREE.Vector3(to.x,.12,to.z)
  ];
  const mat=new THREE.LineBasicMaterial({color,transparent:true,opacity:.42,blending:THREE.AdditiveBlending,depthWrite:false});
  const line=new THREE.Line(new THREE.BufferGeometry().setFromPoints(points),mat);
  line.userData={created:Date.now(),lifetime:9000,type:'trail'};
  line.renderOrder=5;
  scene.add(line);
  transientEffects.push(line);

  const dot=new THREE.Mesh(new THREE.CircleGeometry(.14,24),new THREE.MeshBasicMaterial({color,transparent:true,opacity:.28,blending:THREE.AdditiveBlending,depthWrite:false,side:THREE.DoubleSide}));
  dot.rotation.x=-Math.PI/2;
  dot.position.set(to.x,.1,to.z);
  dot.userData={created:Date.now(),lifetime:9000,type:'trailDot'};
  dot.renderOrder=5;
  scene.add(dot);
  transientEffects.push(dot);
}

function moveTrackedVisual(t,pos,color,rotationY=t.human.rotation.y){
  const prev=t.human.position.clone();
  createHumanTrail(prev,pos,color);
  new TWEEN.Tween(t.human.position).to({x:pos.x,y:pos.y,z:pos.z},620).easing(TWEEN.Easing.Cubic.Out).start();
  new TWEEN.Tween(t.human.rotation).to({y:rotationY},620).easing(TWEEN.Easing.Cubic.Out).start();
  new TWEEN.Tween(t.label.position).to({x:pos.x,y:pos.y,z:pos.z},620).easing(TWEEN.Easing.Cubic.Out).start();
  new TWEEN.Tween(t.ring.position).to({x:pos.x,y:.08,z:pos.z},620).easing(TWEEN.Easing.Cubic.Out).start();
}

function removeTrackedHuman(tid,t){
  scene.remove(t.human,t.label,t.ring);
  const hi=humans.indexOf(t.human);if(hi>=0)humans.splice(hi,1);
  const li=labelGroups.indexOf(t.label);if(li>=0)labelGroups.splice(li,1);
  const ri=groundRings.indexOf(t.ring);if(ri>=0)groundRings.splice(ri,1);
  delete trackedHumans[tid];
}

const MAP_W=2000,MAP_H=700,WORLD_W=26,WORLD_D=13;
const OFFSET_X=0,OFFSET_Z=0;
function p2w(b){
  const cx=b.x+b.width/2,bottom=b.y+b.height;
  return new THREE.Vector3((cx/MAP_W-.5)*WORLD_W+OFFSET_X,0,(bottom/MAP_H-.5)*WORLD_D+OFFSET_Z);
}
function updateCamTarget(bbox,targetId,color,confidence){
  if(!camTargetLayer||!bbox)return;
  const r=(color>>16)&255,g=(color>>8)&255,b=color&255;
  const left=THREE.MathUtils.clamp((bbox.x/MAP_W)*100,0,100);
  const top=THREE.MathUtils.clamp((bbox.y/MAP_H)*100,0,100);
  const width=THREE.MathUtils.clamp((bbox.width/MAP_W)*100,3,100-left);
  const height=THREE.MathUtils.clamp((bbox.height/MAP_H)*100,6,100-top);
  camTargetLayer.innerHTML=`<div class="cam-box" data-id="${escapeHtml(targetId)}" data-conf="CONF ${Math.round(confidence*100)}%"></div>`;
  const box=camTargetLayer.firstElementChild;
  box.style.left=left+'%';
  box.style.top=top+'%';
  box.style.width=width+'%';
  box.style.height=height+'%';
  box.style.setProperty('--target-color',`rgb(${r}, ${g}, ${b})`);
  box.style.setProperty('--target-bg',`rgba(${r}, ${g}, ${b}, .12)`);
  box.style.setProperty('--target-glow',`rgba(${r}, ${g}, ${b}, .36)`);
}

const connEl=document.getElementById('conn'),listEl=document.getElementById('alarms');
const llmCard=document.getElementById('llm-card');
const llmTxt=llmCard.querySelector('.txt'),llmHd=llmCard.querySelector('.hd');
const camImg=document.getElementById('cam-img');
const camTargetLayer=document.getElementById('cam-target');
const sT=document.getElementById('s-t'),sA=document.getElementById('s-a'),sC=document.getElementById('s-c');
const fT=document.getElementById('f-t'),topT=document.getElementById('top-time');
const fpsEl=document.getElementById('m-fps');
const statusCore=document.getElementById('status-core');
const statusCoreState=statusCore.querySelector('.core-state');
const statusCoreKicker=statusCore.querySelector('.core-kicker');
const healthEls={
  updated:document.getElementById('health-updated'),
  api:document.getElementById('h-api'),
  ws:document.getElementById('h-ws'),
  camera:document.getElementById('h-camera'),
  db:document.getElementById('h-db'),
  approval:document.getElementById('h-approval')
};
const flowSteps=[...document.querySelectorAll('.flow-step')];
const flowArrows=[...document.querySelectorAll('.flow-arr')];
const chainEls={
  source:document.getElementById('chain-source'),
  eventId:document.getElementById('chain-event-id'),
  approvalId:document.getElementById('chain-approval-id'),
  toolsCount:document.getElementById('chain-tools-count'),
  timeline:document.getElementById('event-timeline'),
  steps:{
    sense:document.querySelector('[data-chain="sense"]'),
    llm:document.querySelector('[data-chain="llm"]'),
    dispatch:document.querySelector('[data-chain="dispatch"]'),
    tools:document.querySelector('[data-chain="tools"]'),
    approval:document.querySelector('[data-chain="approval"]'),
    execute:document.querySelector('[data-chain="execute"]')
  }
};
let ws,total=0,crit=0,first=true,lastImageUrl='',trendChart,isFocusing=false;
const WS_URL=`ws://${window.location.hostname||'localhost'}:5001`;
const API_URL=`http://${window.location.hostname||'localhost'}:5000`;
const CAMERA_STREAM_URL=`${API_URL}/camera/stream`;
const processedEventIds=new Set();
const shownApprovalIds=new Set();
const shownEvidenceIds=new Set();
const eventTargetMap=new Map();
let trustState={};
camImg.src=CAMERA_STREAM_URL;
camImg.onerror=()=>{setHealthItem(healthEls.camera,'OFF','bad')};

function setFlowState(stage='edge'){
  const idx={cam:0,edge:1,llm:2,decision:3}[stage]??1;
  flowSteps.forEach((step,i)=>{
    step.classList.toggle('done',i<idx);
    step.classList.toggle('active',i===idx);
    step.classList.toggle('pending',i>idx);
    step.classList.toggle('decision',stage==='decision'&&i===idx);
  });
  flowArrows.forEach((arr,i)=>arr.classList.toggle('done',i<idx));
}
setFlowState('edge');

function _chainSet(name,state,label){
  const el=chainEls.steps[name];
  if(!el)return;
  el.className=`chain-step ${state||'pending'}`;
  const meta=el.querySelector('em');
  if(meta)meta.textContent=label||'--';
}
function _statusText(status){
  return {pending:'待审批',approved:'已授权',rejected:'已驳回',auto:'自动放行'}[status]||status||'自动/待定';
}
function _executionText(status){
  return {executed:'已执行',cancelled:'已取消',failed:'执行失败'}[status]||status||'待授权';
}
function _toolName(action){
  return [action?.tool,action?.action].filter(Boolean).join('.')||'tool';
}
function _mergeTrustData(data){
  const base=data.event_id&&trustState.event_id&&data.event_id!==trustState.event_id?{}:trustState;
  const next={...base,...data};
  if(data.events)next.events=data.events;
  if(data.actions)next.actions=data.actions;
  if(data.llm_recommendation)next.llm_recommendation=data.llm_recommendation;
  if(data.dispatch_decision)next.dispatch_decision=data.dispatch_decision;
  if(data.timeline){
    const seen=new Set();
    next.timeline=[...(base.timeline||[]),...(data.timeline||[])].filter(item=>{
      const key=`${item.stage||''}|${item.timestamp||''}|${item.detail||''}`;
      if(seen.has(key))return false;
      seen.add(key);
      return true;
    }).slice(-12);
  }
  trustState=next;
  return trustState;
}
function _timeShort(ts){
  const raw=String(ts||'');
  const m=raw.match(/(\d{2}:\d{2})(?::\d{2})?/);
  return m?m[1]:'--:--';
}
function renderEventTimeline(items=[]){
  if(!chainEls.timeline)return;
  const rows=items.slice(-6);
  if(!rows.length){
    chainEls.timeline.innerHTML='<div class="timeline-row"><time>--:--</time><div><strong>等待事件</strong><span>感知、分析、裁决、执行链路将在这里同步</span></div></div>';
    return;
  }
  chainEls.timeline.innerHTML=rows.map(item=>`<div class="timeline-row"><time>${escapeHtml(_timeShort(item.timestamp))}</time><div><strong>${escapeHtml(item.label||item.stage||'事件')}</strong><span>${escapeHtml(item.detail||'状态已更新')}</span></div></div>`).join('');
  chainEls.timeline.scrollTop=chainEls.timeline.scrollHeight;
}
function updateTrustChain(data={},sourceLabel='实时推送'){
  const state=_mergeTrustData(data);
  const source=data._restored?'历史恢复':sourceLabel;
  const eventId=state.event_id||'--';
  const events=state.events||[];
  const actions=state.actions||[];
  const decision=state.dispatch_decision||{};
  const rec=state.llm_recommendation||{};
  const approvalStatus=state.approval_status||(_hasIntercepted(actions)?'pending':'auto');
  const toolSummary=actions.length?`${actions.length}/${actions.length}`:'0/0';
  if(chainEls.source)chainEls.source.textContent=source.toUpperCase();
  if(chainEls.eventId)chainEls.eventId.textContent=`事件 ${eventId}`;
  if(chainEls.approvalId)chainEls.approvalId.textContent=state.approval_id||'--';
  if(chainEls.toolsCount)chainEls.toolsCount.textContent=toolSummary;
  renderEventTimeline(state.timeline||[]);

  const firstLevel=events[0]?.level||decision.final_level||rec.risk_level||'--';
  _chainSet('sense',events.length?'done':'pending',events.length?`${events.length}项 / ${firstLevel}级`:'待接收');

  const hasLlm=Boolean(state.llm_analysis||state.text||Object.keys(rec).length);
  const llmTimeout=String(state.llm_analysis||'').includes('LLM状态')||String(state.llm_analysis||'').includes('超时');
  const llmLevel=rec.risk_level||decision.llm_level||'--';
  _chainSet('llm',hasLlm?(llmTimeout?'warn':'done'):'pending',hasLlm?`建议 ${llmLevel}`:'待分析');

  const hasDecision=Boolean(Object.keys(decision).length);
  const dispatchLabel=hasDecision?`${decision.rule_level||'--'}+${decision.llm_level||'--'}→${decision.final_level||'--'}`:'待调度';
  _chainSet('dispatch',hasDecision?'done':'pending',dispatchLabel);

  const failed=actions.some(a=>String(a.result||'').includes('FAIL')||String(a.result||'').includes('失败'));
  const actionLabel=actions.length?actions.map(_toolName).slice(0,2).join(' / '):'待执行';
  _chainSet('tools',actions.length?(failed?'warn':'done'):'pending',actionLabel);

  const approvalClass=approvalStatus==='approved'?'approved':approvalStatus==='rejected'?'alert':approvalStatus==='pending'?'warn':'done';
  _chainSet('approval',events.length||hasDecision?approvalClass:'pending',_statusText(approvalStatus));

  const executionStatus=state.execution_status||'';
  const hasExecution=Boolean(executionStatus||state.execution_result);
  const executionClass=executionStatus==='executed'?'approved':executionStatus==='cancelled'?'alert':executionStatus==='failed'?'alert':'done';
  _chainSet('execute',hasExecution?executionClass:(approvalStatus==='approved'?'warn':'pending'),hasExecution?_executionText(executionStatus):'待授权');
}

function updateClock(){
  const t=new Date().toLocaleTimeString('zh-CN',{hour12:false});
  fT.textContent=t;
  topT.textContent=t;
}
updateClock();
setInterval(updateClock,1000);

function setHealthItem(el,value,state='ok'){
  if(!el)return;
  el.classList.remove('ok','warn','bad');
  el.classList.add(state);
  const strong=el.querySelector('strong');
  if(strong)strong.textContent=value;
}
function setHealthHint(el,text){
  const span=el?.querySelector('span');
  if(span)span.textContent=text;
}
async function refreshHealth(){
  try{
    const resp=await fetch(`${API_URL}/health`,{cache:'no-store'});
    if(!resp.ok)throw new Error(`HTTP ${resp.status}`);
    const data=await resp.json();
    const services=data.services||{};
    const wsInfo=services.websocket||{};
    const camera=services.camera||{};
    const db=services.database||{};
    const approval=services.approval||{};
    setHealthItem(healthEls.api,'OK','ok');
    setHealthItem(healthEls.ws,String(wsInfo.clients??0),(ws&&ws.readyState===WebSocket.OPEN)?'ok':'warn');
    setHealthItem(healthEls.camera,camera.status==='online'?`${camera.fps||0} FPS`:'OFF',camera.status==='online'?'ok':'bad');
    setHealthHint(healthEls.camera,camera.status==='online'?`${camera.stream||'RTSP'} · ${_timeShort(camera.last_frame_at)} · R${camera.reconnects??0}`:'实时视频');
    setHealthItem(healthEls.db,`${db.today??0}/${db.total??0}`,'ok');
    setHealthItem(healthEls.approval,String(approval.pending??0),(approval.pending??0)>0?'warn':'ok');
    if(healthEls.updated)healthEls.updated.textContent=_timeShort(data.timestamp);
  }catch(e){
    setHealthItem(healthEls.api,'DOWN','bad');
    setHealthItem(healthEls.ws,'--','bad');
    setHealthItem(healthEls.camera,'--','bad');
    setHealthHint(healthEls.camera,'实时视频');
    if(healthEls.updated)healthEls.updated.textContent='OFFLINE';
  }
}
setTimeout(refreshHealth,800);
setInterval(refreshHealth,5000);

function initChart(){
  const target=document.getElementById('trend-chart');
  if(!window.echarts){
    target.innerHTML='<div class="empty">图表库未加载</div>';
    return;
  }
  trendChart=echarts.init(target,null,{renderer:'canvas'});
  trendChart.setOption({
    backgroundColor:'transparent',
    tooltip:{trigger:'axis',backgroundColor:'rgba(8,13,13,.94)',borderColor:'rgba(122,238,214,.25)',textStyle:{color:'#e8f4ef'}},
    grid:{left:34,right:16,top:22,bottom:24},
    xAxis:{type:'category',boundaryGap:false,data:['00','02','04','06','08','10','12','14','16','18','20','22'],axisLine:{lineStyle:{color:'rgba(255,255,255,.12)'}},axisTick:{show:false},axisLabel:{color:'rgba(232,244,239,.42)',fontSize:10}},
    yAxis:{type:'value',minInterval:1,splitLine:{lineStyle:{color:'rgba(255,255,255,.07)'}},axisLabel:{color:'rgba(232,244,239,.42)',fontSize:10}},
    series:[{name:'违规',type:'line',smooth:true,symbol:'circle',symbolSize:5,data:[2,1,0,1,3,5,4,6,8,7,5,4],lineStyle:{color:'#ffb74a',width:2},areaStyle:{color:new echarts.graphic.LinearGradient(0,0,0,1,[{offset:0,color:'rgba(255,183,74,.34)'},{offset:1,color:'rgba(255,183,74,.02)'}])},itemStyle:{color:'#ffb74a'}}]
  });
}
setTimeout(initChart,260);

function resizeRenderer(){
  const w=W(),h=H();
  renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,1.75));
  renderer.setSize(w,h,false);
  composer.setSize(w,h);
  labelRenderer.setSize(w,h);
  camera.aspect=w/h;
  camera.updateProjectionMatrix();
  trendChart?.resize();
}
new ResizeObserver(resizeRenderer).observe(container);
window.addEventListener('resize',resizeRenderer);

function escapeHtml(value){
  return String(value??'').replace(/[&<>"']/g,s=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
}
function formatTargetId(value){
  const raw=String(value??'');
  if(/^T[-_\s]?\d{3,8}$/i.test(raw))return raw.replace(/[_\s]/g,'-').toUpperCase();
  let hash=0;
  for(let i=0;i<raw.length;i++)hash=(hash*31+raw.charCodeAt(i))%10000;
  return 'T-'+String(hash||8204).padStart(4,'0');
}
function highlightTargetRefs(html){
  return String(html).replace(/\b(?:TARGET[:\s-]*)?(T[-_]?\d{3,8}|pos_\d+_\d+)\b/gi,m=>`<span class="target-ref">${escapeHtml(m)}</span>`);
}
function safeMarkdown(text){
  const escaped=escapeHtml(text||'');
  const html=window.marked?marked.parse(escaped):escaped.replace(/\n/g,'<br>');
  return highlightTargetRefs(html);
}
function renderLLM(text){
  llmTxt.innerHTML=safeMarkdown(text||'');
  llmTxt.classList.remove('llm-in');
  void llmTxt.offsetWidth;
  llmTxt.classList.add('llm-in');
  llmCard.scrollTop=0;
}
function speak(text){
  try{
    const u=new SpeechSynthesisUtterance(text);
    u.lang='zh-CN';
    u.rate=1.08;
    speechSynthesis.cancel();
    speechSynthesis.speak(u);
  }catch(e){}
}
function _hasIntercepted(actions){return actions&&actions.some(a=>a.tool==='human_loop'&&String(a.result||'').includes('拦截'))}
function _showApprovalCard(approvalId='',eventId=''){
  const approvalKey=approvalId||eventId||'local';
  if(approvalKey&&shownApprovalIds.has(approvalKey))return;
  if(approvalKey)shownApprovalIds.add(approvalKey);
  setFlowState('decision');
  const card=document.createElement('div');
  card.className='card A approval-card';
  card.dataset.approvalId=approvalId||'';
  card.dataset.eventId=eventId||'';
  card.innerHTML=`<div class="t"><span>A级高危事件 · 待安全员审批</span><span class="b A">A 级</span></div>
    <div class="d">系统已拦截自动关停指令</div>
    <div class="approval-actions">
      <button type="button" class="cyber-btn reject">驳回指令</button>
      <button type="button" class="cyber-btn auth">授权执行</button>
    </div>`;
  const submitApproval=async(action)=>{
    if(!approvalId){
      return {status:'error',result:'未收到审批工单编号，无法提交后端审批'};
    }
    const resp=await fetch(`${API_URL}/approval/${action}`,{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({approval_id:approvalId,event_id:eventId,operator:'frontend'})
    });
    const result=await resp.json().catch(()=>({status:'error',result:`HTTP ${resp.status}`}));
    if(!resp.ok||result.status!=='ok')throw new Error(result.message||result.result||`HTTP ${resp.status}`);
    return result;
  };
  card.querySelectorAll('button')[0].onclick=async()=>{
    card.querySelectorAll('button').forEach(btn=>btn.disabled=true);
    try{
      const result=await submitApproval('reject');
      updateTrustChain(result,'审批回传');
      updateEventLifecycle(result);
      card.innerHTML=`<div style="color:var(--red);border-left:3px solid var(--red);padding:8px 8px 8px 10px;font-weight:700;background:rgba(255,92,101,.1);font-family:'DIN Alternate','Consolas',monospace;letter-spacing:1px">
        [REJECTED] ${escapeHtml(result.result||'处置指令已驳回')}
      </div>`;
      setTimeout(()=>{card.remove();setFlowState('edge')},2600);
      refreshHealth();
    }catch(e){
      card.querySelectorAll('button').forEach(btn=>btn.disabled=false);
      card.querySelector('.d').textContent=`审批提交失败：${e.message}`;
    }
  };
  card.querySelectorAll('button')[1].onclick=async()=>{
    card.querySelectorAll('button').forEach(btn=>btn.disabled=true);
    card.innerHTML=`<div style="font-family:'Consolas','DIN Alternate',monospace;font-size:11px;color:var(--cyan);line-height:1.6;letter-spacing:.4px">
      &gt; INIT SAFETY OVERRIDE... [OK]<br>
      &gt; SUBMIT HUMAN_APPROVAL TO BACKEND...<br>
      <span style="color:var(--amber);animation:cursor-blink .5s infinite">WAITING EDGE ACK...</span>
    </div>`;
    try{
      const result=await submitApproval('approve');
      updateTrustChain(result,'审批回传');
      updateEventLifecycle(result);
      card.innerHTML=`<div style="color:var(--green);border-left:3px solid var(--green);padding:8px 8px 8px 10px;font-weight:700;background:rgba(120,208,141,.1);font-family:'DIN Alternate','Consolas',monospace;letter-spacing:1px">
        [ACK] ${escapeHtml(result.result||'审批通过，处置指令已确认')}
      </div>`;
      setTimeout(()=>{card.remove();setFlowState('edge')},3000);
      refreshHealth();
    }catch(e){
      card.innerHTML=`<div style="color:var(--red);border-left:3px solid var(--red);padding:8px 8px 8px 10px;font-weight:700;background:rgba(255,92,101,.1);font-family:'DIN Alternate','Consolas',monospace;letter-spacing:1px">
        [FAILED] ${escapeHtml(e.message)}
      </div>`;
      if(approvalKey)shownApprovalIds.delete(approvalKey);
      setTimeout(()=>_showApprovalCard(approvalId,eventId),1800);
    }
  };
  listEl.prepend(card);
  listEl.scrollTop=0;
}
function _buildActionsDiv(actions){
  const ab=document.createElement('div');
  ab.style.cssText='color:rgba(98,243,221,.68);margin-top:8px;padding-top:8px;border-top:1px solid rgba(98,243,221,.16);font-size:11px';
  ab.innerHTML=highlightTargetRefs('【决策执行】<br>'+actions.map(a=>'▸ '+escapeHtml(a.tool)+'.'+escapeHtml(a.action)+' → '+escapeHtml(a.result)).join('<br>'));
  return ab;
}
function _buildDecisionDiv(decision){
  if(!decision||typeof decision!=='object'||!Object.keys(decision).length)return null;
  const rule=escapeHtml(decision.rule_level||'无');
  const llm=escapeHtml(decision.llm_level||'无');
  const finalLevel=escapeHtml(decision.final_level||'无');
  const policy=escapeHtml(decision.policy||'规则约束调度');
  const adopted=decision.llm_adopted?'已采纳':'未采纳';
  const reason=decision.reason?`<br>▸ 裁决依据：${escapeHtml(decision.reason)}`:'';
  const confidence=Number.isFinite(Number(decision.confidence))?`<br>▸ LLM置信度：${Number(decision.confidence).toFixed(2)}`:'';
  const div=document.createElement('div');
  div.style.cssText='color:rgba(255,183,74,.82);margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,183,74,.18);font-size:11px;line-height:1.65';
  div.innerHTML=`【调度裁决】<br>▸ 规则等级 ${rule} + LLM建议 ${llm} → 最终 ${finalLevel}<br>▸ 裁决策略：${policy}<br>▸ LLM建议：${adopted}${confidence}${reason}`;
  return div;
}

function setView(next){
  const presets={
    home:{theta:.72,phi:1.05,dist:52,target:new THREE.Vector3(0,1.2,0)},
    top:{theta:.05,phi:.18,dist:64,target:new THREE.Vector3(0,0,0)},
    close:{theta:.95,phi:1.18,dist:30,target:new THREE.Vector3(0,4,0)}
  };
  const p=presets[next]||presets.home;
  const state={theta:ca.theta,phi:ca.phi,dist:cd,x:camTarget.x,y:camTarget.y,z:camTarget.z};
  new TWEEN.Tween(state).to({theta:p.theta,phi:p.phi,dist:p.dist,x:p.target.x,y:p.target.y,z:p.target.z},780)
    .easing(TWEEN.Easing.Cubic.InOut)
    .onUpdate(()=>{ca.theta=state.theta;ca.phi=state.phi;cd=state.dist;camTarget.set(state.x,state.y,state.z)})
    .start();
}
document.querySelectorAll('#view-controls button').forEach(btn=>btn.addEventListener('click',()=>setView(btn.dataset.view)));

async function triggerDemoScenario(scenario,btn){
  const statusEl=document.getElementById('demo-status');
  const buttons=[...document.querySelectorAll('.demo-btn')];
  buttons.forEach(item=>item.disabled=true);
  if(statusEl)statusEl.textContent=`正在注入 ${scenario}...`;
  try{
    const resp=await fetch(`${API_URL}/demo/trigger`,{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({scenario})
    });
    const data=await resp.json().catch(()=>({status:'error',message:`HTTP ${resp.status}`}));
    if(!resp.ok||data.status!=='ok')throw new Error(data.message||`HTTP ${resp.status}`);
    if(statusEl)statusEl.textContent=`已注入 ${data.event_id||scenario}`;
    setFlowState('cam');
    refreshHealth();
  }catch(e){
    if(statusEl)statusEl.textContent=`演示触发失败：${e.message}`;
  }finally{
    setTimeout(()=>buttons.forEach(item=>item.disabled=false),900);
  }
}
document.querySelectorAll('.demo-btn').forEach(btn=>btn.addEventListener('click',()=>triggerDemoScenario(btn.dataset.demo,btn)));

function _focusCameraOn(pos){
  if(isFocusing)return;
  isFocusing=true;
  defaultTarget.copy(camTarget);
  defaultDist=cd;
  defaultTheta=ca.theta;
  defaultPhi=ca.phi;
  const state={x:camTarget.x,y:camTarget.y,z:camTarget.z,d:cd,theta:ca.theta,phi:ca.phi};
  new TWEEN.Tween(state).to({x:pos.x,y:5.2,z:pos.z,d:26,theta:ca.theta+.18,phi:1.06},1300)
    .easing(TWEEN.Easing.Cubic.InOut)
    .onUpdate(()=>{camTarget.set(state.x,state.y,state.z);cd=state.d;ca.theta=state.theta;ca.phi=state.phi})
    .chain(new TWEEN.Tween(state).to({x:defaultTarget.x,y:defaultTarget.y,z:defaultTarget.z,d:defaultDist,theta:defaultTheta,phi:defaultPhi},1700)
      .easing(TWEEN.Easing.Quadratic.InOut).delay(3600)
      .onUpdate(()=>{camTarget.set(state.x,state.y,state.z);cd=state.d;ca.theta=state.theta;ca.phi=state.phi})
      .onComplete(()=>{isFocusing=false}))
    .start();
}

function updateTrend(){
  if(!trendChart)return;
  const opt=trendChart.getOption();
  const data=opt.series[0].data.slice();
  const bucket=Math.floor(new Date().getHours()/2);
  data[bucket]=(Number(data[bucket])||0)+1;
  trendChart.setOption({series:[{data}]});
}

function normalizeViolations(ev){
  const type=String(ev.type||'');
  const violations=[];
  if(type.includes('安全帽'))violations.push('no_helmet');
  if(type.includes('背心')||type.includes('反光'))violations.push('no_vest');
  if(type.includes('火焰')||type.includes('烟'))violations.push('fire');
  if(type.includes('区域')||type.includes('入侵')||type.includes('危险区')||type.includes('车辆通道'))violations.push('intrusion');
  if(type.includes('人车')||type.includes('接近')||type.includes('混行')||type.includes('距离'))violations.push('proximity');
  return violations;
}

function stateForLevel(level,violations=[]){
  if(level==='A')return 'critical';
  if(violations.length||level==='B')return 'warning';
  return 'active';
}

function setHumanStateVisual(t,state){
  if(!t)return;
  t.state=state;
  const opacityByState={active:1,warning:1,critical:1,pending_approval:1,decided:1,approved:.78,rejected:.5,stale:.46,resolved:0};
  const targetOpacity=opacityByState[state]??1;
  for(const root of [t.human,t.label,t.ring]){
    if(!root)continue;
    root.traverse?.(o=>{
      if(o.element)o.element.style.opacity=String(targetOpacity);
      const materials=o.material?Array.isArray(o.material)?o.material:[o.material]:[];
      for(const mat of materials){
        if(!mat.transparent)mat.transparent=true;
        const base=mat.userData?.baseOpacity??mat.opacity??1;
        mat.userData.baseOpacity=base;
        new TWEEN.Tween(mat).to({opacity:base*targetOpacity},520).easing(TWEEN.Easing.Quadratic.Out).start();
      }
    });
  }
}

function lifecycleStyle(status,level='B'){
  const normalized=String(status||'').toLowerCase();
  if(normalized==='pending_approval'||normalized==='pending')return {state:'pending_approval',color:0xffb74a,label:'PENDING APPROVAL',core:'等待人工审批'};
  if(normalized==='approved')return {state:'approved',color:0x78d08d,label:'APPROVED',core:'处置授权完成'};
  if(normalized==='executed')return {state:'approved',color:0x78d08d,label:'EXECUTED',core:'执行处置完成'};
  if(normalized==='rejected')return {state:'rejected',color:0xff5c65,label:'REJECTED',core:'处置指令驳回'};
  if(normalized==='decided')return {state:'decided',color:level==='A'?0xff5c65:level==='B'?0xffb74a:0x62f3dd,label:'DISPATCHED',core:'调度裁决完成'};
  if(normalized==='analyzing')return {state:stateForLevel(level),color:level==='A'?0xff5c65:level==='B'?0xffb74a:0x62f3dd,label:'ANALYZING',core:'Agent分析中'};
  return {state:stateForLevel(level),color:level==='A'?0xff5c65:level==='B'?0xffb74a:0x62f3dd,label:'DETECTED',core:'目标已检测'};
}

function updateLifecycleLabel(t,label,color){
  const r=(color>>16)&255,g=(color>>8)&255,b=color&255;
  t.label?.traverse?.(o=>{
    if(!o.element)return;
    o.element.style.setProperty('--label-color',`rgb(${r}, ${g}, ${b})`);
    o.element.style.setProperty('--label-bg',`rgba(${r}, ${g}, ${b}, 0.25)`);
    let state=o.element.querySelector('.state');
    if(!state){
      state=document.createElement('span');
      state.className='state';
      o.element.appendChild(state);
    }
    state.textContent=label;
  });
}

function createLifecyclePulse(pos,color,status){
  if(!pos)return;
  const isApproved=status==='approved'||status==='executed';
  const radius=isApproved?0.72:0.92;
  const opacity=isApproved?0.5:0.62;
  const ring=new THREE.Mesh(
    new THREE.TorusGeometry(radius,.035,14,72),
    new THREE.MeshBasicMaterial({color,transparent:true,opacity,blending:THREE.AdditiveBlending,depthWrite:false})
  );
  ring.rotation.x=-Math.PI/2;
  ring.position.set(pos.x,.12,pos.z);
  ring.userData={created:Date.now(),lifetime:isApproved?1800:2200,type:'lifecycle',baseOpacity:opacity};
  ring.renderOrder=7;
  scene.add(ring);
  transientEffects.push(ring);
}

function applyLifecycleToTarget(t,status,data={}){
  if(!t)return;
  const previousStatus=t.lifecycleStatus;
  const level=(data.dispatch_decision?.final_level||data.events?.[0]?.level||t.level||'B').toUpperCase();
  const style=lifecycleStyle(status,level);
  t.lifecycleStatus=status;
  recolorHumanSignal(t.human,style.color);
  if(t.ring?.material){
    t.ring.material.color?.setHex(style.color);
    t.ring.material.opacity=style.state==='approved'?0.22:style.state==='rejected'?0.16:0.34;
  }
  updateLifecycleLabel(t,style.label,style.color);
  setHumanStateVisual(t,style.state);
  setCoreStatus(level,style.core);
  statusCore.classList.toggle('approved',status==='approved'||status==='executed');
  if(status==='approved'||status==='executed'){
    statusCore.classList.remove('alert','warn');
  }else if(status==='pending_approval'||status==='pending'){
    statusCore.classList.remove('alert','approved');
    statusCore.classList.add('warn');
  }else if(status==='rejected'){
    statusCore.classList.remove('warn','approved');
    statusCore.classList.add('alert');
  }else{
    statusCore.classList.remove('approved');
  }
  if(previousStatus!==status&&(status==='approved'||status==='executed'||status==='rejected'||status==='pending_approval')){
    createLifecyclePulse(t.human.position,style.color,status);
  }
}

function bindEventTarget(eventId,tid){
  if(!eventId||!tid)return;
  if(!eventTargetMap.has(eventId))eventTargetMap.set(eventId,new Set());
  eventTargetMap.get(eventId).add(tid);
}

function eventTargetIds(data={}){
  const ids=new Set(eventTargetMap.get(data.event_id)||[]);
  for(const ev of data.events||[]){
    const bbox=normalizeBBox(ev.bbox||{x:1000,y:350,width:80,height:160});
    ids.add(trackKeyFor(ev,bbox,data));
  }
  return [...ids];
}

function updateEventLifecycle(data={}){
  const status=data.lifecycle_status||(data.approval_status==='pending'?'pending_approval':data.approval_status)||'detected';
  const ids=eventTargetIds(data);
  ids.forEach(tid=>applyLifecycleToTarget(trackedHumans[tid],status,data));
}

function setCoreStatus(level,text){
  statusCoreState.textContent=text;
  statusCoreKicker.textContent=level==='A'?'CRITICAL TARGET LOCKED':level==='B'?'VIOLATION TRACKING':'SECURITY TWIN ONLINE';
  statusCore.classList.remove('approved');
  statusCore.classList.toggle('alert',level==='A');
  statusCore.classList.toggle('warn',level==='B');
}

function createAlertImpact(pos,color){
  const shock=new THREE.Mesh(new THREE.TorusGeometry(.8,.04,14,96),new THREE.MeshBasicMaterial({color,transparent:true,opacity:.68,blending:THREE.AdditiveBlending,depthWrite:false}));
  shock.rotation.x=-Math.PI/2;
  shock.position.set(pos.x,.13,pos.z);
  shock.userData={created:Date.now(),lifetime:1500,type:'shock'};
  scene.add(shock);
  transientEffects.push(shock);

  const beam=new THREE.Mesh(new THREE.CylinderGeometry(.055,.055,8,20,1,true),new THREE.MeshBasicMaterial({color,transparent:true,opacity:.24,side:THREE.DoubleSide,blending:THREE.AdditiveBlending,depthWrite:false}));
  beam.position.set(pos.x,4,pos.z);
  beam.userData={created:Date.now(),lifetime:1900,type:'beam'};
  beam.renderOrder=6;
  scene.add(beam);
  transientEffects.push(beam);
}

function addAlarm(data,ev){
  total++;
  if(ev.level==='A')crit++;
  sT.textContent=total;
  sC.textContent=crit;
  const rawLevel=String(ev.level||'B').toUpperCase();
  const level=['A','B','C'].includes(rawLevel)?rawLevel:'B';
  const eventType=escapeHtml(ev.type||'安全事件');
  const eventDetail=escapeHtml(ev.detail||'已定位到场景坐标');
  const eventTime=escapeHtml(data.timestamp||new Date().toLocaleString('zh-CN',{hour12:false}));
  const bbox=normalizeBBox(ev.bbox||{x:1000,y:350,width:80,height:160});
  const tid=trackKeyFor(ev,bbox,data);
  bindEventTarget(data.event_id,tid);
  const targetId=formatTargetId(tid);
  const camId=escapeHtml(ev.cameraId||data.cameraId||'CAM-01');
  const confRaw=Number(ev.confidence??ev.score??ev.conf??(.86+Math.random()*.09));
  const confidence=Math.max(0,Math.min(1,confRaw));
  const statusText=level==='A'?'待审批':level==='B'?'已拦截':'跟踪中';
  const evidenceKey=data.event_id||data.image_url||'';
  const showEvidence=Boolean(data.image_url)&&!shownEvidenceIds.has(evidenceKey);
  if(showEvidence)shownEvidenceIds.add(evidenceKey);
  const evidenceHtml=showEvidence?`<div class="evidence-shot"><img src="${escapeHtml(data.image_url)}" alt="报警证据截图" loading="lazy"></div>`:'';
  const card=document.createElement('div');
  card.className=`card ${level}`;
  card.innerHTML=`<div class="t"><span>${eventType}</span><span class="b ${level}">${escapeHtml(level)} 级</span></div><div class="d">${eventDetail}</div>
    ${evidenceHtml}
    <div class="meta"><span>目标 <strong>${escapeHtml(targetId)}</strong></span><span>相机 <strong>${camId}</strong></span><span>置信 <strong>${Math.round(confidence*100)}%</strong></span><span>状态 <strong>${statusText}</strong></span></div>
    <div class="m">${eventTime}</div>`;
  listEl.prepend(card);
  while(listEl.children.length>32)listEl.lastChild.remove();

  const violations=normalizeViolations(ev);
  const placement=displayPositionFor(tid,bbox,data);
  const pos=placement.position;
  const rotationY=placement.rotationY;
  const color=level==='A'?0xff5c65:level==='B'?0xffb74a:0x62f3dd;
  const nextState=stateForLevel(level,violations);
  updateCamTarget(bbox,targetId,color,confidence);
  setCoreStatus(level,level==='A'?'高危目标锁定':level==='B'?'违规目标跟踪':'全域巡检中');
  if(level==='A'){
    _focusCameraOn(pos);
    createAlertImpact(pos,color);
  }
  const existing=trackedHumans[tid];
  if(existing){
    const mergedViolations=[...existing.violations];
    let visualChanged=existing.color!==color||existing.level!==level;
    for(const v of violations){
      if(!mergedViolations.includes(v)){
        mergedViolations.push(v);
        visualChanged=true;
      }
    }
    if(visualChanged){
      const previousPos=existing.human.position.clone();
      scene.remove(existing.human);
      const oldHumanIndex=humans.indexOf(existing.human);
      if(oldHumanIndex>=0)humans.splice(oldHumanIndex,1);
      existing.human=createHuman(pos,mergedViolations,color,rotationY);
      humans.push(existing.human);
      createHumanTrail(previousPos,pos,color);
    }else{
      moveTrackedVisual(existing,pos,color,rotationY);
      recolorHumanSignal(existing.human,color);
    }
    if(existing.ring.material){
      existing.ring.material.color?.setHex(color);
      existing.ring.material.opacity=.34;
    }
    existing.ring.position.copy(pos);
    existing.ring.userData.created=Date.now();
    existing.lastSeen=Date.now();
    existing.violations=mergedViolations;
    existing.color=color;
    existing.level=level;
    existing.rotationY=rotationY;
    existing.targetId=targetId;
    scene.remove(existing.label);
    const oldLabelIndex=labelGroups.indexOf(existing.label);
    if(oldLabelIndex>=0)labelGroups.splice(oldLabelIndex,1);
    existing.label=createLabel(pos,existing.violations,color,targetId);
    labelGroups.push(existing.label);
    existing.resolvedAt=0;
    setHumanStateVisual(existing,nextState);
  }else{
    const h=createHuman(pos,violations,color,rotationY);
    const l=createLabel(pos,violations,color,targetId);
    const r=createGroundRing(pos,color);
    humans.push(h);
    labelGroups.push(l);
    groundRings.push(r);
    trackedHumans[tid]={human:h,label:l,ring:r,lastSeen:Date.now(),createdAt:Date.now(),violations,color,level,targetId,state:nextState,resolvedAt:0,rotationY};
    setHumanStateVisual(trackedHumans[tid],nextState);
  }
  sA.textContent=Object.keys(trackedHumans).length;
  alertLight.color.setHex(color);
  alertLight.intensity=level==='A'?24:16;
  updateTrend();
}

function eventKeyForData(data){
  return data.event_id||`${data.timestamp||''}:${(data.events||[]).map(e=>e.type).join('|')}`;
}
function renderEventData(data){
  updateTrustChain(data,data._restored?'历史恢复':'实时推送');
  if(!data.events?.length){
    updateEventLifecycle(data);
    return;
  }
  const key=eventKeyForData(data);
  if(key&&processedEventIds.has(key)){
    updateEventLifecycle(data);
    return;
  }
  if(key)processedEventIds.add(key);
  if(first){listEl.innerHTML='';first=false}
  if(data.image_url&&data.image_url!==lastImageUrl)lastImageUrl=data.image_url;
  data.events.forEach(ev=>addAlarm(data,ev));
  updateEventLifecycle(data);
}
function handleMessage(raw){
  const data=typeof raw==='string'?JSON.parse(raw):raw;
  if(data.type==='approval_result'){
    updateTrustChain(data,'审批回传');
    updateEventLifecycle(data);
    const status=data.approval_status==='approved'?'已授权执行':'已驳回指令';
    const exec=data.execution_status?`\n【执行回写】${_executionText(data.execution_status)}：${data.execution_result||''}`:'';
    renderLLM(`${llmTxt.textContent}\n\n【审批结果】${status}：${data.result||''}${exec}`);
    return;
  }
  if(first){listEl.innerHTML='';first=false}
  if(data.type==='alarm_with_llm'){
    // 只更新 LLM，事件已在第一次广播处理过，return 防止重复计数
    renderEventData(data);
    if(data.llm_analysis){
      setFlowState('llm');
      llmHd.textContent='Qwen2.5-VL 视觉分析';
      renderLLM(data.llm_analysis);
      const decisionDiv=_buildDecisionDiv(data.dispatch_decision);
      if(decisionDiv)llmTxt.appendChild(decisionDiv);
      if(data.actions?.length)llmTxt.appendChild(_buildActionsDiv(data.actions));
      const approvalStatus=data.approval_status||'pending';
      if(_hasIntercepted(data.actions)&&approvalStatus==='pending')_showApprovalCard(data.approval_id||'',data.event_id||'');
      speak((data.llm_analysis||'').split('\n')[0]?.slice(0,100)||'');
    }
    return;
  }else if(data.type==='llm'){
    updateTrustChain(data,'LLM分析');
    setFlowState('llm');
    llmHd.textContent='Qwen2.5-VL 视觉分析';
    renderLLM(data.text);
    if(data.actions?.length)llmTxt.appendChild(_buildActionsDiv(data.actions));
    if(_hasIntercepted(data.actions))_showApprovalCard();
    speak((data.text||'').split('\n')[0]?.slice(0,100)||'');
    return;
  }
  if(!data.events)return;
  renderEventData(data);
}

function connectWS(){
  try{ws=new WebSocket(WS_URL)}catch(e){return}
  ws.onopen=()=>{connEl.innerHTML='<span class="dot"></span>已连接';connEl.className='chip c';setFlowState('edge');setHealthItem(healthEls.ws,'1+','ok');refreshHealth()};
  ws.onclose=()=>{connEl.innerHTML='<span class="dot"></span>重连中';connEl.className='chip dc';setFlowState('cam');setHealthItem(healthEls.ws,'0','warn');setTimeout(connectWS,2000)};
  ws.onerror=()=>ws.close();
  ws.onmessage=e=>handleMessage(e.data);
}
connectWS();

async function restoreRecentEvents(){
  try{
    const resp=await fetch(`${API_URL}/recent_alarms?limit=5`);
    const data=await resp.json();
    const items=[...(data.events||[])].reverse();
    items.forEach(item=>handleMessage({...item,_restored:true}));
  }catch(e){}
}
restoreRecentEvents();

let dragging=false,pm={x:0,y:0};
container.addEventListener('pointerdown',e=>{
  if(e.target!==renderer.domElement)return;
  dragging=true;
  container.setPointerCapture(e.pointerId);
  pm={x:e.clientX,y:e.clientY};
});
container.addEventListener('pointerup',e=>{
  dragging=false;
  try{container.releasePointerCapture(e.pointerId)}catch(err){}
});
container.addEventListener('pointercancel',()=>dragging=false);
container.addEventListener('pointermove',e=>{
  if(!dragging)return;
  ca.theta-=(e.clientX-pm.x)*.004;
  ca.phi-=(e.clientY-pm.y)*.004;
  ca.phi=Math.max(.16,Math.min(1.48,ca.phi));
  pm={x:e.clientX,y:e.clientY};
});
container.addEventListener('wheel',e=>{
  e.preventDefault();
  cd=Math.max(16,Math.min(112,cd+e.deltaY*.045));
},{passive:false});
container.addEventListener('contextmenu',e=>e.preventDefault());

const clock=new THREE.Clock();
let fpsAcc=0,fpsFrames=0,fpsTimer=performance.now();
function animate(){
  requestAnimationFrame(animate);
  const dt=Math.min(clock.getDelta(),.1),now=Date.now();
  TWEEN.update();
  const sp=Math.sin(ca.phi),cp=Math.cos(ca.phi);
  camera.position.set(camTarget.x+cd*sp*Math.sin(ca.theta),camTarget.y+cd*cp,camTarget.z+cd*sp*Math.cos(ca.theta));
  camera.lookAt(camTarget);
  particles.rotation.y+=dt*.014;
  zones.forEach(z=>z.rotation.y+=z.userData.spin);
  for(const flow of dataFlows){
    flow.t=(flow.t+dt*flow.speed)%1;
    const p=flow.curve.getPointAt(flow.t);
    flow.dot.position.set(p.x,.22+Math.sin(now*.006+flow.t*8)*.08,p.z);
  }
  for(const ring of scanWaves){
    ring.userData.phase=(ring.userData.phase+dt*.12)%1;
    const phase=ring.userData.phase;
    ring.scale.setScalar(2+phase*24);
    ring.material.opacity=.18*(1-phase);
  }
  for(const h of humans){
    h.traverse(o=>{
      if(o.userData?.isHUD){
        o.rotation.z+=o.userData.rotSpeed;
        o.scale.setScalar(1+Math.sin(now*o.userData.pulseSpeed*.05)*.14);
      }
      if(o.userData?.isTargetLock){
        const base=o.userData.baseOpacity??.72;
        o.material.opacity=base*(.72+Math.sin(now*(o.userData.pulseSpeed||.006))*.18);
      }
      if(o.userData?.isHumanScan){
        const height=o.userData.height||3;
        const phase=((now*.00055)+(o.userData.phase||0))%1;
        o.position.y=.32+phase*Math.max(height-.62,1);
        o.material.opacity=(o.userData.baseOpacity||.2)*(1-Math.abs(phase-.5)*1.15);
      }
      if(o.userData?.isRiskBeacon){
        const pulse=.82+Math.sin(now*(o.userData.pulseSpeed||.006))*.18;
        o.position.y=(o.userData.baseY||3.2)+Math.sin(now*.003)*.06;
        o.scale.setScalar(pulse);
      }
      if(o.userData?.isChestSweep){
        const t=(Math.sin(now*(o.userData.pulseSpeed||.008))+1)/2;
        o.position.y=(o.userData.baseY||1.2)+t*(o.userData.range||.7);
        o.material.opacity=.18+.2*Math.sin(now*.012)*Math.sin(now*.012);
      }
      if(o.userData?.isIntrusionZone){
        o.rotation.z+=o.userData.rotSpeed||.015;
        o.material.opacity=(o.userData.baseOpacity||.2)*(.68+Math.sin(now*.005)*.24);
      }
      if(o.userData?.isProximityLine){
        o.material.opacity=(o.userData.baseOpacity||.62)*(.62+Math.sin(now*.012)*.3);
        o.scale.x=1+Math.sin(now*.01)*.06;
      }
      if(o.userData?.isHeatColumn){
        o.material.opacity=(o.userData.baseOpacity||.18)*(.72+Math.sin(now*.009)*.22);
        o.scale.set(1+Math.sin(now*.006)*.08,1,1+Math.cos(now*.007)*.08);
      }
      if(o.userData?.isFirePulse){
        const phase=(now*.0012)%1;
        o.scale.setScalar(1+phase*1.8);
        o.material.opacity=(o.userData.baseOpacity||.28)*(1-phase);
      }
      if(o.userData?.isRadarSweep){
        o.rotation.z+=o.userData.rotSpeed||.02;
        o.material.opacity=(o.userData.baseOpacity||.34)*(.72+Math.sin(now*.004)*.16);
      }
    });
  }
  for(let i=transientEffects.length-1;i>=0;i--){
    const o=transientEffects[i];
    const age=now-o.userData.created;
    const life=o.userData.lifetime||1500;
    const t=Math.min(age/life,1);
    if(o.userData.type==='shock'){
      o.scale.setScalar(1+t*5.2);
      o.material.opacity=.68*(1-t);
    }else if(o.userData.type==='beam'){
      o.scale.set(1+t*.65,1,1+t*.65);
      o.material.opacity=.24*(1-t);
    }else if(o.userData.type==='trail'||o.userData.type==='trailDot'){
      o.material.opacity=(o.userData.type==='trail'?0.42:.28)*(1-t);
    }else if(o.userData.type==='lifecycle'){
      o.scale.setScalar(1+t*2.4);
      o.material.opacity=(o.userData.baseOpacity||o.material.opacity||.5)*(1-t);
    }
    if(t>=1){
      scene.remove(o);
      transientEffects.splice(i,1);
    }
  }
  for(let i=groundRings.length-1;i>=0;i--){
    const r=groundRings[i];
    const age=now-r.userData.created;
    const life=r.userData.lifetime||HUMAN_RING_TTL;
    r.scale.setScalar(1+(age/life)*2.2);
    r.material.opacity=Math.max(0,.28*(1-age/life));
    if(age>life&&r.parent===scene){scene.remove(r);groundRings.splice(i,1)}
  }
  for(const[tid,t]of Object.entries(trackedHumans)){
    const silentFor=now-t.lastSeen;
    const terminal=t.lifecycleStatus==='approved'||t.lifecycleStatus==='executed'||t.lifecycleStatus==='rejected';
    if(silentFor>HUMAN_TRACK_TTL){
      if(!t.resolvedAt){
        t.resolvedAt=now;
        setHumanStateVisual(t,'resolved');
      }else if(now-t.resolvedAt>HUMAN_FADE_DURATION){
        removeTrackedHuman(tid,t);
      }
    }else if(!terminal&&silentFor>HUMAN_STALE_AFTER&&t.state!=='stale'){
      setHumanStateVisual(t,'stale');
    }
  }
  sA.textContent=Object.keys(trackedHumans).length;
  alertLight.intensity=THREE.MathUtils.lerp(alertLight.intensity,12,dt*1.5);
  composer.render();
  labelRenderer.render(scene,camera);

  fpsAcc+=1/Math.max(dt,.001);
  fpsFrames++;
  if(performance.now()-fpsTimer>800){
    fpsEl.textContent=Math.round(fpsAcc/fpsFrames)+' FPS';
    fpsAcc=0;
    fpsFrames=0;
    fpsTimer=performance.now();
  }
}
resizeRenderer();
animate();
