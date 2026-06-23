import { useEffect, useRef, useState } from "react";
import * as PIXI from "pixi.js";

// ─── Scene Constants ───────────────────────────────────────

const FLOOR_COLOR = 0xd4b896; // Light wood
const GRID_COLOR = 0xc4a886;
const WALL_COLOR = 0x8b7355;

// Furniture dimensions
const DESK_W = 80;
const DESK_H = 40;
const CHAIR_SIZE = 28;
const MONITOR_W = 36;
const MONITOR_H = 24;

// ─── Workstation layout ────────────────────────────────────

interface Workstation {
  id: string;
  deskX: number;
  deskY: number;
  chairX: number;
  chairY: number;
  monitorX: number;
  monitorY: number;
}

const WORKSTATIONS: Workstation[] = [
  // Top row — 3 desks
  { id: "ws1", deskX: 180, deskY: 80, chairX: 206, chairY: 125, monitorX: 202, monitorY: 72 },
  { id: "ws2", deskX: 320, deskY: 80, chairX: 346, chairY: 125, monitorX: 342, monitorY: 72 },
  { id: "ws3", deskX: 460, deskY: 80, chairX: 486, chairY: 125, monitorX: 482, monitorY: 72 },
  // Bottom row — 2 desks
  { id: "ws4", deskX: 180, deskY: 280, chairX: 206, chairY: 325, monitorX: 202, monitorY: 272 },
  { id: "ws5", deskX: 320, deskY: 280, chairX: 346, chairY: 325, monitorX: 342, monitorY: 272 },
];

// Character sprite frame size (after processing, individual frame PNGs)
const CHAR_FRAME_W = 548;
const CHAR_FRAME_H = 1632;
const CHAR_DISPLAY_H = 80; // Display height in scene
const CHAR_DISPLAY_W = Math.round(CHAR_DISPLAY_H * (CHAR_FRAME_W / CHAR_FRAME_H));
const WALK_SPEED = 2; // pixels per frame

// ─── AgentSprite ───────────────────────────────────────────

class AgentSprite {
  container: PIXI.Container;
  private sprite: PIXI.Sprite;
  private frames: PIXI.Texture[] = [];
  private state: "idle" | "walking" | "sitting" | "typing" = "idle";
  private frameIndex = 0;
  private frameTimer = 0;
  private frameDelay = 8; // game ticks between frame changes
  private targetX = 0;
  private targetY = 0;
  private onArrived: (() => void) | null = null;
  private walkFrames: PIXI.Texture[] = [];

  constructor() {
    this.container = new PIXI.Container();
    this.sprite = new PIXI.Sprite(PIXI.Texture.WHITE);
    this.sprite.tint = 0xff6666; // Placeholder red tint
    this.sprite.width = CHAR_DISPLAY_W;
    this.sprite.height = CHAR_DISPLAY_H;
    this.container.addChild(this.sprite);
  }

  async loadFrames() {
    // Load walk sprite sheet (7 frames horizontal)
    try {
      this.walkFrames = await this.loadSpriteSheet("/sprites/raw/walk.png", 7);
      this.frames = this.walkFrames;
      if (this.walkFrames.length > 0) {
        this.sprite.texture = this.walkFrames[0];
        this.sprite.tint = 0xffffff;
        this.fitSprite();
      }
    } catch {
      console.warn("Walk sprite sheet not found, using placeholder");
    }
  }

  private async loadSpriteSheet(url: string, frameCount: number): Promise<PIXI.Texture[]> {
    // 1. Load image into an HTML Image element
    const img = new Image();
    img.crossOrigin = "anonymous";
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve();
      img.onerror = () => reject(new Error(`Failed to load ${url}`));
      img.src = url;
    });

    // 2. Draw to offscreen canvas and remove dark background
    const canvas = document.createElement("canvas");
    canvas.width = img.width;
    canvas.height = img.height;
    const ctx = canvas.getContext("2d")!;
    ctx.drawImage(img, 0, 0);

    const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const data = imageData.data;

    // Sample corners to determine background color
    const sample = (offset: number) => [data[offset], data[offset + 1], data[offset + 2]];
    const tl = sample(0);
    const tr = sample((canvas.width - 1) * 4);
    const bl = sample((canvas.height - 1) * canvas.width * 4);
    const br = sample(((canvas.height - 1) * canvas.width + (canvas.width - 1)) * 4);
    const bgR = Math.round((tl[0] + tr[0] + bl[0] + br[0]) / 4);
    const bgG = Math.round((tl[1] + tr[1] + bl[1] + br[1]) / 4);
    const bgB = Math.round((tl[2] + tr[2] + bl[2] + br[2]) / 4);

    const TOLERANCE = 55; // How similar to bg color before making transparent
    for (let i = 0; i < data.length; i += 4) {
      const dr = data[i] - bgR;
      const dg = data[i + 1] - bgG;
      const db = data[i + 2] - bgB;
      const dist = Math.sqrt(dr * dr + dg * dg + db * db);
      if (dist < TOLERANCE) {
        data[i + 3] = 0; // Make transparent
      } else if (dist < TOLERANCE * 1.5) {
        // Feather edge — partial transparency for smoother edges
        data[i + 3] = Math.round(((dist - TOLERANCE) / (TOLERANCE * 0.5)) * 255);
      }
    }
    ctx.putImageData(imageData, 0, 0);

    // 3. Create PixiJS texture from processed canvas
    const baseTexture = PIXI.Texture.from(canvas);
    const frameWidth = Math.floor(baseTexture.width / frameCount);
    const frameHeight = baseTexture.height;
    const textures: PIXI.Texture[] = [];

    for (let i = 0; i < frameCount; i++) {
      const rect = new PIXI.Rectangle(i * frameWidth, 0, frameWidth, frameHeight);
      textures.push(new PIXI.Texture({ source: baseTexture.source, frame: rect }));
    }
    return textures;
  }

  private fitSprite() {
    const tex = this.sprite.texture;
    if (tex && tex.width > 0 && tex.height > 0) {
      const scale = CHAR_DISPLAY_H / tex.height;
      this.sprite.width = tex.width * scale;
      this.sprite.height = CHAR_DISPLAY_H;
    }
  }

  setPosition(x: number, y: number) {
    this.container.x = x;
    this.container.y = y;
  }

  walkTo(x: number, y: number): Promise<void> {
    return new Promise((resolve) => {
      this.state = "walking";
      this.targetX = x;
      this.targetY = y;
      this.frames = this.walkFrames;
      this.frameIndex = 0;
      this.onArrived = resolve;
    });
  }

  sitDown() {
    this.state = "sitting";
    // Use first walk frame as sitting placeholder
    if (this.walkFrames.length > 0) {
      this.sprite.texture = this.walkFrames[0];
      this.fitSprite();
    }
  }

  startTyping() {
    this.state = "typing";
    this.frames = this.walkFrames;
    this.frameIndex = 0;
  }

  standUp() {
    this.state = "idle";
    this.frames = this.walkFrames;
    this.frameIndex = 0;
  }

  update() {
    // Handle walking movement
    if (this.state === "walking") {
      const dx = this.targetX - this.container.x;
      const dy = this.targetY - this.container.y;
      const dist = Math.sqrt(dx * dx + dy * dy);

      if (dist < WALK_SPEED) {
        this.container.x = this.targetX;
        this.container.y = this.targetY;
        this.state = "idle";
        this.frameIndex = 0;
        if (this.frames.length > 0) {
          this.sprite.texture = this.frames[0];
          this.fitSprite();
        }
        if (this.onArrived) {
          this.onArrived();
          this.onArrived = null;
        }
      } else {
        this.container.x += (dx / dist) * WALK_SPEED;
        this.container.y += (dy / dist) * WALK_SPEED;
      }

      // Flip sprite based on direction
      if (dx < 0) {
        this.sprite.scale.x = -Math.abs(this.sprite.scale.x);
      } else if (dx > 0) {
        this.sprite.scale.x = Math.abs(this.sprite.scale.x);
      }
    }

    // Animate frames
    if (this.state === "walking" || this.state === "typing") {
      this.frameTimer++;
      if (this.frameTimer >= this.frameDelay && this.frames.length > 0) {
        this.frameTimer = 0;
        this.frameIndex = (this.frameIndex + 1) % this.frames.length;
        this.sprite.texture = this.frames[this.frameIndex];
        this.fitSprite();
      }
    }
  }
}

// ─── Furniture Rendering ───────────────────────────────────

function createFloor(app: PIXI.Application): PIXI.Container {
  const floor = new PIXI.Container();

  // Main floor area
  const floorGfx = new PIXI.Graphics();
  floorGfx.rect(0, 0, 700, 420);
  floorGfx.fill(FLOOR_COLOR);
  floor.addChild(floorGfx);

  // Grid lines
  const grid = new PIXI.Graphics();
  for (let x = 0; x <= 700; x += 40) {
    grid.moveTo(x, 0);
    grid.lineTo(x, 420);
  }
  for (let y = 0; y <= 420; y += 40) {
    grid.moveTo(0, y);
    grid.lineTo(700, y);
  }
  grid.stroke({ width: 1, color: GRID_COLOR, alpha: 0.3 });
  floor.addChild(grid);

  // Walls
  const walls = new PIXI.Graphics();
  // Top wall
  walls.rect(0, 0, 700, 30);
  walls.fill(WALL_COLOR);
  // Left wall
  walls.rect(0, 0, 30, 420);
  walls.fill(WALL_COLOR);
  floor.addChild(walls);

  return floor;
}

function createDesk(x: number, y: number): PIXI.Graphics {
  const desk = new PIXI.Graphics();
  desk.rect(x, y, DESK_W, DESK_H);
  desk.fill(0x8B6914); // Brown wood
  desk.stroke({ width: 2, color: 0x6B4F12 });
  return desk;
}

function createChair(x: number, y: number): PIXI.Graphics {
  const chair = new PIXI.Graphics();
  chair.rect(x, y, CHAIR_SIZE, CHAIR_SIZE);
  chair.fill(0x404050); // Dark gray
  chair.stroke({ width: 1, color: 0x303040 });
  // Chair back
  chair.rect(x, y - 4, CHAIR_SIZE, 6);
  chair.fill(0x505060);
  return chair;
}

function createMonitor(x: number, y: number): PIXI.Container {
  const monitor = new PIXI.Container();
  const gfx = new PIXI.Graphics();

  // Screen
  gfx.rect(x, y, MONITOR_W, MONITOR_H);
  gfx.fill(0x1a1a2e);
  gfx.stroke({ width: 2, color: 0x333355 });

  // Screen glow (blue tint)
  const screen = new PIXI.Graphics();
  screen.rect(x + 3, y + 3, MONITOR_W - 6, MONITOR_H - 8);
  screen.fill({ color: 0x2244aa, alpha: 0.4 });

  // Stand
  const stand = new PIXI.Graphics();
  stand.rect(x + MONITOR_W / 2 - 4, y + MONITOR_H, 8, 6);
  stand.fill(0x333333);

  monitor.addChild(gfx);
  monitor.addChild(screen);
  monitor.addChild(stand);
  return monitor;
}

function createBookshelf(x: number, y: number): PIXI.Graphics {
  const shelf = new PIXI.Graphics();
  // Main body
  shelf.rect(x, y, 30, 80);
  shelf.fill(0x6B4226);
  shelf.stroke({ width: 2, color: 0x4A2E18 });
  // Shelves
  for (let sy = 0; sy < 4; sy++) {
    shelf.rect(x + 2, y + sy * 20 + 2, 26, 2);
    shelf.fill(0x8B5E3C);
    // Books (colored rectangles)
    const colors = [0xcc3333, 0x3366cc, 0x33aa33, 0xcccc33, 0xcc66cc];
    for (let b = 0; b < 3; b++) {
      shelf.rect(x + 4 + b * 8, y + sy * 20 + 5, 6, 14);
      shelf.fill(colors[(sy * 3 + b) % colors.length]);
    }
  }
  return shelf;
}

function createPlant(x: number, y: number): PIXI.Graphics {
  const plant = new PIXI.Graphics();
  // Pot
  plant.rect(x + 4, y + 16, 17, 14);
  plant.fill(0xB5651D);
  // Leaves
  plant.circle(x + 12, y + 10, 12);
  plant.fill(0x2E8B57);
  plant.circle(x + 6, y + 6, 8);
  plant.fill(0x3CB371);
  plant.circle(x + 18, y + 8, 9);
  plant.fill(0x228B22);
  return plant;
}

function createWaterCooler(x: number, y: number): PIXI.Graphics {
  const cooler = new PIXI.Graphics();
  // Body
  cooler.rect(x, y + 15, 25, 30);
  cooler.fill(0xCCCCCC);
  cooler.stroke({ width: 1, color: 0x999999 });
  // Water bottle
  cooler.rect(x + 6, y, 13, 18);
  cooler.fill(0x88BBEE);
  cooler.stroke({ width: 1, color: 0x6699CC });
  // Tap
  cooler.rect(x + 9, y + 25, 7, 4);
  cooler.fill(0x3366CC);
  return cooler;
}

function createMeetingTable(x: number, y: number): PIXI.Graphics {
  const table = new PIXI.Graphics();
  table.rect(x, y, 120, 70);
  table.fill(0x8B6914);
  table.stroke({ width: 2, color: 0x6B4F12 });
  return table;
}

function createFurnitureLayer(): PIXI.Container {
  const layer = new PIXI.Container();

  // Bookshelves (left wall)
  layer.addChild(createBookshelf(40, 50));
  layer.addChild(createBookshelf(40, 150));

  // Water cooler
  layer.addChild(createWaterCooler(45, 210));

  // Plants
  layer.addChild(createPlant(42, 330));
  layer.addChild(createPlant(560, 50));

  // Workstations (desks + chairs + monitors)
  for (const ws of WORKSTATIONS) {
    layer.addChild(createDesk(ws.deskX, ws.deskY));
    layer.addChild(createChair(ws.chairX, ws.chairY));
    layer.addChild(createMonitor(ws.monitorX, ws.monitorY));
  }

  // Meeting table (bottom right)
  layer.addChild(createMeetingTable(460, 280));

  return layer;
}

// ─── Test Sequence ─────────────────────────────────────────

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function runTestSequence(agent: AgentSprite, setLog: (msg: string) => void) {
  const ws1 = WORKSTATIONS[0];
  const ws2 = WORKSTATIONS[3];

  // Start at entrance
  agent.setPosition(40, 380);
  setLog("Agent appeared at entrance (idle)");
  await delay(1500);

  // Walk to workstation 1
  setLog("Walking to workstation 1...");
  await agent.walkTo(ws1.chairX, ws1.chairY + CHAIR_SIZE);
  setLog("Arrived at workstation 1");
  await delay(500);

  // Sit down
  agent.sitDown();
  setLog("Sitting down...");
  await delay(1000);

  // Start typing
  agent.startTyping();
  setLog("Typing at workstation 1...");
  await delay(3000);

  // Stand up
  agent.standUp();
  setLog("Standing up...");
  await delay(500);

  // Walk to workstation 4
  setLog("Walking to workstation 4...");
  await agent.walkTo(ws2.chairX, ws2.chairY + CHAIR_SIZE);
  setLog("Arrived at workstation 4");
  await delay(500);

  // Sit and type again
  agent.sitDown();
  setLog("Sitting down at workstation 4...");
  await delay(1000);

  agent.startTyping();
  setLog("Typing at workstation 4...");
  await delay(2000);

  setLog("Test sequence complete! Click 'Run Test' to replay.");
}

// ─── OfficeView Component ──────────────────────────────────

export default function OfficeView() {
  const containerRef = useRef<HTMLDivElement>(null);
  const appRef = useRef<PIXI.Application | null>(null);
  const agentRef = useRef<AgentSprite | null>(null);
  const [log, setLog] = useState("Loading...");
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    if (!containerRef.current) return;

    const app = new PIXI.Application();
    appRef.current = app;
    let initialized = false;
    let destroyed = false;

    app.init({
      width: 700,
      height: 420,
      background: 0x1a1f2b,
      antialias: true,
      resolution: window.devicePixelRatio || 1,
      autoDensity: true,
    }).then(async () => {
      // Guard: if cleanup already ran (StrictMode unmount), skip setup
      if (destroyed || !containerRef.current) return;
      initialized = true;
      containerRef.current.appendChild(app.canvas);

      try {
        // 1. Floor
        const floor = createFloor(app);
        app.stage.addChild(floor);

        // 2. Furniture
        const furniture = createFurnitureLayer();
        app.stage.addChild(furniture);

        // 3. Character
        const agent = new AgentSprite();
        agent.setPosition(40, 380);
        app.stage.addChild(agent.container);
        agentRef.current = agent;

        // Load sprite frames
        await agent.loadFrames();
        setLog("Character loaded. Click 'Run Test' to start.");

        // Game loop — update agent
        app.ticker.add(() => {
          agent.update();
        });
      } catch (err: any) {
        console.error("Scene error:", err);
        setError(`Scene error: ${err?.message || String(err)}`);
      }
    }).catch((err: any) => {
      if (destroyed) return; // Ignore if already cleaned up
      console.error("PixiJS init error:", err);
      setError(`PixiJS init failed: ${err?.message || String(err)}`);
    });

    return () => {
      destroyed = true;
      if (appRef.current && initialized) {
        try {
          appRef.current.destroy(true);
        } catch (e) {
          console.warn("PixiJS destroy error:", e);
        }
      }
      appRef.current = null;
    };
  }, []);

  const handleRunTest = async () => {
    if (running || !agentRef.current) return;
    setRunning(true);
    await runTestSequence(agentRef.current, setLog);
    setRunning(false);
  };

  return (
    <div className="flex flex-col h-full relative">
      {/* Toolbar */}
      <div className="px-3 py-2 border-b border-surface-border flex items-center gap-3">
        <button
          onClick={handleRunTest}
          disabled={running}
          className="px-3 py-1 text-xs rounded-md bg-accent/20 text-accent hover:bg-accent/30 disabled:opacity-50 transition-colors"
        >
          {running ? "Running..." : "Run Test"}
        </button>
        <span className="text-xs text-gray-400 truncate flex-1">{log}</span>
      </div>

      {/* PixiJS Canvas */}
      <div
        ref={containerRef}
        className="flex-1 flex items-center justify-center bg-[#0f1117] overflow-hidden"
      />

      {/* Error overlay */}
      {error && (
        <div className="absolute inset-0 flex items-center justify-center bg-red-900/20 pointer-events-none">
          <div className="bg-red-900/80 text-red-200 px-4 py-2 rounded-lg text-xs max-w-md text-center">
            {error}
          </div>
        </div>
      )}
    </div>
  );
}
