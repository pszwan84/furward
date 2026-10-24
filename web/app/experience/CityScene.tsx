'use client';

/* eslint-disable react-hooks/set-state-in-effect -- Renderer initialization reports WebGL capability and resource-loading state from the external Three.js lifecycle. */

import { useEffect, useRef, useState, type RefObject } from 'react';
import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { angleDifference, pointAtDistance, type DemoState, type DemoWorld } from '@/lib/demo-types';
import { createDemoFrameClock } from '@/lib/demo-frame-clock';
import { collectFootContacts, groundFootContacts, restoreFootContacts, type FootContact } from '@/lib/foot-contact';
import styles from './experience.module.css';

type Props = {
  world: DemoWorld;
  stateRef: RefObject<DemoState | null>;
  receivedAt: RefObject<number>;
  view: 'follow' | 'overview';
};
type Actor = {
  root: THREE.Group;
  mixer: THREE.AnimationMixer;
  actions: Record<string, THREE.AnimationAction>;
  current: string;
  turnUntil: number;
  previousHeading: number;
  nominalSpeed: number;
  feet: FootContact[];
};

export default function CityScene({ world, stateRef, receivedAt, view }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const viewRef = useRef(view);
  const [phase, setPhase] = useState('正在载入城市与伙伴…');
  const [failure, setFailure] = useState('');
  const [attempt, setAttempt] = useState(0);
  useEffect(() => { viewRef.current = view; }, [view]);

  useEffect(() => {
    const container = host.current;
    if (!container) return;
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, powerPreference: 'high-performance' });
    } catch {
      setFailure('这台设备暂时无法显示三维城市。路线与文字操作仍可使用。');
      return;
    }
    let cancelled = false;
    let frame = 0;
    const frameClock = createDemoFrameClock();
    let priorView = '';
    let priorStatus = '';
    let snapCamera = true;
    const actors: Actor[] = [];
    const groundMeshes: THREE.Mesh[] = [];
    const cityBounds = new THREE.Box3();
    const sceneryMeshes: THREE.Mesh[] = [];
    const fadedMeshes = new Map<THREE.Mesh, THREE.Material | THREE.Material[]>();
    const fadedMaterials = new Map<THREE.Mesh, THREE.Material | THREE.Material[]>();
    const sightRay = new THREE.Raycaster();
    const surfaceMarks: THREE.Mesh[] = [];
    const groundRay = new THREE.Raycaster();
    const down = new THREE.Vector3(0, -1, 0);
    const floorAt = (x: number, z: number, fallback: number) => {
      groundRay.set(new THREE.Vector3(x, 6, z), down);
      const hit = groundRay.intersectObjects(groundMeshes, false)[0];
      return hit?.point.y ?? fallback;
    };
    const scene = new THREE.Scene();
    scene.background = new THREE.Color('#e9e8df');
    const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 2500);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.maxPolarAngle = Math.PI * 0.47;
    controls.minDistance = 5;
    controls.maxDistance = 1500;
    controls.enablePan = true;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.75));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.1;
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFShadowMap;
    renderer.domElement.setAttribute('aria-label', '三维城市：绒途在前，人物沿路线跟随。可切换全景并拖动查看。');
    renderer.domElement.setAttribute('role', 'img');
    container.appendChild(renderer.domElement);
    const ambient = new THREE.HemisphereLight('#fff4df', '#a49d9e', 2.2);
    scene.add(ambient);
    const routeBox = new THREE.Box3().setFromPoints(world.route.points.map(p => new THREE.Vector3(p.x, p.y, p.z)));
    const center = routeBox.getCenter(new THREE.Vector3());
    const sun = new THREE.DirectionalLight('#fff1dc', 2.6);
    sun.position.copy(center).add(new THREE.Vector3(-60, 100, 30));
    sun.target.position.copy(center);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    sun.shadow.camera.left = -110;
    sun.shadow.camera.right = 110;
    sun.shadow.camera.top = 110;
    sun.shadow.camera.bottom = -110;
    sun.shadow.camera.near = 1;
    sun.shadow.camera.far = 280;
    sun.shadow.bias = -0.0005;
    sun.shadow.normalBias = 0.12;
    scene.add(sun, sun.target);

    const routeMaterial = new THREE.MeshBasicMaterial({ color: '#d2ff00', depthWrite: false });
    // Each asset and route point is already in the same Y-up world; no centering/scaling.
    const pointMarker = (distance: number, color: string) => {
      const p = pointAtDistance(world.route.points, distance).point;
      const marker = new THREE.Mesh(new THREE.RingGeometry(0.7, 0.9, 40), new THREE.MeshBasicMaterial({ color, side: THREE.DoubleSide }));
      marker.rotation.x = -Math.PI / 2;
      marker.position.set(p.x, p.y + 0.08, p.z);
      surfaceMarks.push(marker);
      scene.add(marker);
    };
    pointMarker(0, '#25281f');
    pointMarker(world.route.total_distance_m, '#d2ff00');
    const obstacle = new THREE.Group();
    const obstacleBoard = new THREE.Mesh(new THREE.BoxGeometry(1.45, 0.7, 0.2), new THREE.MeshStandardMaterial({ color: '#e9a64d', roughness: 0.9 }));
    obstacleBoard.position.y = 0.65;
    obstacle.add(obstacleBoard);
    for (const x of [-0.55, 0.55]) {
      const leg = new THREE.Mesh(new THREE.BoxGeometry(0.13, 0.6, 0.35), new THREE.MeshStandardMaterial({ color: '#3c3b34' }));
      leg.position.set(x, 0.3, 0);
      obstacle.add(leg);
    }
    const obstaclePoint = pointAtDistance(world.route.points, world.route.obstacle_distance_m + 1.8);
    obstacle.position.set(obstaclePoint.point.x, obstaclePoint.point.y, obstaclePoint.point.z);
    obstacle.rotation.y = obstaclePoint.heading;
    obstacle.visible = false;
    scene.add(obstacle);

    const disposeObject = (root: THREE.Object3D) => {
      const textures = new Set<THREE.Texture>();
      const materials = new Set<THREE.Material>();
      const geometries = new Set<THREE.BufferGeometry>();
      root.traverse(object => {
        if (!(object instanceof THREE.Mesh)) return;
        geometries.add(object.geometry);
        for (const material of Array.isArray(object.material) ? object.material : [object.material]) {
          materials.add(material);
          for (const value of Object.values(material)) if (value instanceof THREE.Texture) textures.add(value);
        }
        if (object instanceof THREE.SkinnedMesh) object.skeleton.dispose();
      });
      geometries.forEach(geometry => geometry.dispose());
      textures.forEach(texture => { if (typeof ImageBitmap !== 'undefined' && texture.source.data instanceof ImageBitmap) texture.source.data.close(); texture.dispose(); });
      materials.forEach(material => material.dispose());
    };
    const loader = new GLTFLoader();
    const rigInfo = fetch('/city/animated/rig-manifest.json').then(async response => {
      if (!response.ok) throw new Error('伙伴动画资源不完整，请重试。');
      return await response.json() as { models: Record<'human' | 'dog', { nominal_walk_speed_mps: number }> };
    });
    const loadModel = async (url: string, actor: boolean) => {
      const gltf = await loader.loadAsync(url);
      if (cancelled) { disposeObject(gltf.scene); return; }
      gltf.scene.traverse(object => {
        if (object instanceof THREE.Mesh) {
          object.castShadow = true;
          object.receiveShadow = true;
          if (object instanceof THREE.SkinnedMesh) object.frustumCulled = false;
          if (/^(Continuous_Sidewalks|Ground_Extension_Seamless|Extended_Street_Network|Road_Main_4Segments|Crosswalks_7)$/.test(object.name)) groundMeshes.push(object);
          else if (!actor) sceneryMeshes.push(object);
        }
      });
      if (actor) {
        const root = new THREE.Group();
        root.add(gltf.scene);
        const mixer = new THREE.AnimationMixer(gltf.scene);
        const actions: Record<string, THREE.AnimationAction> = {};
        for (const clip of gltf.animations) actions[clip.name] = mixer.clipAction(clip);
        const required = ['idle', 'walk', 'turn_left', 'turn_right', 'stop'];
        if (required.some(name => !actions[name])) { disposeObject(gltf.scene); throw new Error('伙伴动画资源不完整，请重试。'); }
        actions.stop.setLoop(THREE.LoopOnce, 1);
        actions.stop.clampWhenFinished = true;
        const kind = url === world.assets.dog ? 'dog' : 'human';
        const manifest = await rigInfo;
        if (cancelled) { disposeObject(gltf.scene); return; }
        const nominalSpeed = manifest.models[kind].nominal_walk_speed_mps;
        if (!Number.isFinite(nominalSpeed) || nominalSpeed <= 0) { disposeObject(gltf.scene); throw new Error('伙伴动画资源不完整，请重试。'); }
        const feet = collectFootContacts(root);
        if (feet.length !== (kind === 'human' ? 2 : 4)) { disposeObject(gltf.scene); throw new Error('伙伴动画资源不完整，请重试。'); }
        actors.push({ root, mixer, actions, current: '', turnUntil: 0, previousHeading: pointAtDistance(world.route.points, 0).heading, nominalSpeed, feet });
        root.userData.kind = kind;
        scene.add(root);
      } else {
        scene.add(gltf.scene);
        gltf.scene.updateMatrixWorld(true);
        cityBounds.expandByObject(gltf.scene);
        if (viewRef.current === 'overview') priorView = '';
      }
    };
    setPhase('正在载入城市与伙伴…');
    setFailure('');
    Promise.all([
      rigInfo,
      loadModel(world.assets.city, false), loadModel(world.assets.periphery, false),
      loadModel(world.assets.human, true), loadModel(world.assets.dog, true),
    ]).then(() => {
      if (!cancelled) {
        for (const mark of surfaceMarks) mark.position.y = floorAt(mark.position.x, mark.position.z, mark.position.y) + 0.025;
        const path = new THREE.CurvePath<THREE.Vector3>();
        let previous: THREE.Vector3 | undefined;
        // Drape the visible path onto the actual pavement, which has small height steps.
        for (let d = 0; d <= world.route.total_distance_m; d += 0.5) {
          const p = pointAtDistance(world.route.points, d).point;
          const next = new THREE.Vector3(p.x, floorAt(p.x, p.z, p.y) + 0.055, p.z);
          if (previous) path.add(new THREE.LineCurve3(previous, next));
          previous = next;
        }
        const routeOutline = new THREE.Mesh(new THREE.TubeGeometry(path, 600, 0.11, 4, false), new THREE.MeshBasicMaterial({ color: '#454d2f', depthWrite: false }));
        const routeLine = new THREE.Mesh(new THREE.TubeGeometry(path, 600, 0.065, 4, false), routeMaterial);
        // A dark edge keeps lime readable on the warm pavement; normal depth still
        // lets people and buildings occlude the line rather than painting over them.
        routeOutline.renderOrder = 2;
        routeLine.renderOrder = 3;
        scene.add(routeOutline, routeLine);
        setPhase('');
      }
    }).catch(reason => {
      if (!cancelled) { setPhase(''); setFailure(reason instanceof Error && reason.message.includes('动画') ? reason.message : '三维资源载入失败，路线与文字操作仍可使用。'); }
    });
    const resize = new ResizeObserver(() => {
      const width = container.clientWidth, height = container.clientHeight;
      renderer.setSize(width, height);
      camera.aspect = width / Math.max(height, 1);
      camera.updateProjectionMatrix();
      snapCamera = true;
    });
    resize.observe(container);
    const onLost = (event: Event) => { event.preventDefault(); setFailure('三维画面已中断，点击重新载入可恢复。'); };
    renderer.domElement.addEventListener('webglcontextlost', onLost);
    const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
    const desiredCamera = new THREE.Vector3();
    const desiredTarget = new THREE.Vector3();
    const sightDirection = new THREE.Vector3();
    let sightFrame = 0;
    const onVisibilityChange = () => {
      frameClock.reset();
      if (!document.hidden) snapCamera = true;
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    const animate = (now: number) => {
      if (cancelled) return;
      frame = requestAnimationFrame(animate);
      if (document.hidden) return;
      const delta = frameClock.tick(now);
      if (delta === null) return;
      const state = stateRef.current;
      // Prediction is bounded to 250ms; a stopped snapshot is applied exactly, never drifted.
      const age = Math.max(0, (now - receivedAt.current) / 1000);
      const active = state?.status === 'ACTIVE' && age < 1;
      const distance = Math.min(world.route.total_distance_m, (state?.distance_m ?? 0) + (state?.status === 'ACTIVE' ? Math.min(age, 0.25) * world.route.speed_mps : 0));
      const dogPoint = pointAtDistance(world.route.points, distance);
      const stoppedNow = priorStatus === 'ACTIVE' && state?.status !== 'ACTIVE';
      for (const actor of actors) {
        const offset = actor.root.userData.kind === 'human' ? -2.2 : 0;
        const target = pointAtDistance(world.route.points, distance + offset, true);
        actor.root.position.set(target.point.x, floorAt(target.point.x, target.point.z, target.point.y) + 0.003, target.point.z);
        const turn = angleDifference(target.heading, actor.previousHeading);
        if (active && Math.abs(turn) > 0.45) {
          actor.turnUntil = now + 650;
          actor.root.userData.turn = turn > 0 ? 'turn_left' : 'turn_right';
        }
        actor.previousHeading = target.heading;
        if (priorStatus === '' || state?.status === 'IDLE') actor.root.rotation.y = target.heading;
        else actor.root.rotation.y += angleDifference(target.heading, actor.root.rotation.y) * Math.min(1, delta * 8);
        // Translation keeps the calibrated walk stride through a corner. The shorter
        // turn clips are only suitable while stationary, never at the route's 1.2m/s.
        const turningInPlace = !active && actor.turnUntil > now && Math.abs(angleDifference(target.heading, actor.root.rotation.y)) > 0.08;
        let clip = active ? 'walk' : turningInPlace ? actor.root.userData.turn : 'idle';
        if (!active && (stoppedNow || (actor.current === 'stop' && actor.actions.stop.isRunning()))) clip = 'stop';
        if (clip !== actor.current) {
          const next = actor.actions[clip];
          next.reset().setEffectiveTimeScale(clip === 'walk' ? world.route.speed_mps / actor.nominalSpeed : 1).setEffectiveWeight(1).play();
          if (actor.current) actor.actions[actor.current].crossFadeTo(next, 0.22, false);
          actor.current = clip;
        }
        restoreFootContacts(actor.feet);
        actor.mixer.update(delta);
        // Crossfading two valid poses does not preserve their IK foot contacts.
        // Constrain the actual skinned soles after blending, without lifting the
        // whole character or changing the authoritative route distance.
        actor.root.updateMatrixWorld(true);
        groundFootContacts(actor.feet, floorAt, actor.root.position.y - 0.003);
      }
      priorStatus = state?.status ?? '';
      obstacle.visible = !!state?.obstacle_active;
      if (obstacle.visible) {
        const location = pointAtDistance(world.route.points, distance + 1.8);
        obstacle.position.set(location.point.x, floorAt(location.point.x, location.point.z, location.point.y), location.point.z);
        obstacle.rotation.y = location.heading;
      }
      if (viewRef.current !== priorView) {
        priorView = viewRef.current;
        snapCamera = true;
        controls.enabled = priorView === 'overview';
        if (priorView === 'overview') {
          const bounds = cityBounds.isEmpty() ? routeBox : cityBounds;
          const overviewCenter = bounds.getCenter(new THREE.Vector3());
          const radius = bounds.getSize(new THREE.Vector3()).length() * 0.5;
          const fitFov = 2 * Math.atan(Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2) * Math.min(camera.aspect, 1));
          const distanceToFit = radius / Math.sin(fitFov / 2) * 1.02;
          camera.position.copy(overviewCenter).addScaledVector(new THREE.Vector3(0.75, 1.2, 0.8).normalize(), distanceToFit);
          controls.target.copy(overviewCenter);
          controls.update();
        }
      }
      if (viewRef.current === 'follow') {
        desiredTarget.set(dogPoint.point.x, dogPoint.point.y + 1, dogPoint.point.z);
        const heading = actors.find(actor => actor.root.userData.kind === 'dog')?.root.rotation.y ?? dogPoint.heading;
        const backwards = new THREE.Vector3(-Math.sin(heading), 0, -Math.cos(heading));
        const sideways = new THREE.Vector3(Math.cos(heading), 0, -Math.sin(heading));
        desiredCamera.copy(desiredTarget).addScaledVector(backwards, 10).addScaledVector(sideways, 5.5);
        desiredCamera.y += 8;
        camera.position.lerp(desiredCamera, snapCamera || reduced ? 1 : 1 - Math.exp(-delta * 4));
        controls.target.lerp(desiredTarget, snapCamera || reduced ? 1 : 1 - Math.exp(-delta * 7));
        camera.lookAt(controls.target);
        snapCamera = false;
      } else controls.update();
      // Preserve source materials. Only cloned materials of actual camera occluders fade,
      // and the original bindings return as soon as the sight line clears or overview opens.
      if (sightFrame++ % 4 === 0 || viewRef.current === 'overview') {
        const occluded = new Set<THREE.Mesh>();
        if (viewRef.current === 'follow') {
          scene.updateMatrixWorld(true);
          for (const actor of actors) {
            const target = actor.root.position.clone();
            target.y += actor.root.userData.kind === 'human' ? 1 : 0.5;
            const length = camera.position.distanceTo(target);
            sightDirection.subVectors(target, camera.position).normalize();
            sightRay.set(camera.position, sightDirection);
            sightRay.far = Math.max(0, length - 0.4);
            for (const hit of sightRay.intersectObjects(sceneryMeshes, false)) occluded.add(hit.object as THREE.Mesh);
          }
        }
        for (const [mesh, original] of fadedMeshes) {
          if (!occluded.has(mesh)) { mesh.material = original; fadedMeshes.delete(mesh); }
        }
        for (const mesh of occluded) {
          if (fadedMeshes.has(mesh)) continue;
          const original = mesh.material;
          let faded = fadedMaterials.get(mesh);
          if (!faded) {
            const makeFaded = (material: THREE.Material) => {
              const clone = material.clone(); clone.transparent = true; clone.opacity = 0.12; clone.depthWrite = false; return clone;
            };
            faded = Array.isArray(original) ? original.map(makeFaded) : makeFaded(original);
            fadedMaterials.set(mesh, faded);
          }
          fadedMeshes.set(mesh, original);
          mesh.material = faded;
        }
      }
      renderer.render(scene, camera);
    };
    frame = requestAnimationFrame(animate);
    return () => {
      cancelled = true;
      cancelAnimationFrame(frame);
      document.removeEventListener('visibilitychange', onVisibilityChange);
      resize.disconnect();
      controls.dispose();
      actors.forEach(actor => { actor.mixer.stopAllAction(); actor.mixer.uncacheRoot(actor.mixer.getRoot()); });
      for (const [mesh, original] of fadedMeshes) mesh.material = original;
      for (const faded of fadedMaterials.values()) for (const material of Array.isArray(faded) ? faded : [faded]) material.dispose();
      disposeObject(scene);
      sun.shadow.dispose();
      renderer.domElement.removeEventListener('webglcontextlost', onLost);
      renderer.dispose();
      renderer.forceContextLoss();
      renderer.domElement.remove();
    };
  }, [world, stateRef, receivedAt, attempt]);

  return <div className={styles.scene} ref={host}>
    {phase && !failure && <div className={styles.sceneMessage} role="status">{phase}</div>}
    {failure && <div className={styles.sceneMessage} role="status"><p>{failure}</p><button onClick={() => setAttempt(value => value + 1)}>重新载入三维画面</button></div>}
  </div>;
}
