/**
 * Combined WebGL Liquid Chrome & Silk Aurora background shader.
 * Features:
 *  - Liquid metal domain warping & noise waves
 *  - Silk aurora color gradients (cyan & deep gold/violet ribbons)
 *  - Interactive mouse fluid distortion
 *  - Grain & vignette rendering
 */

(function () {
  const vertexShaderSource = `
    attribute vec2 position;
    void main() {
      gl_Position = vec4(position, 0.0, 1.0);
    }
  `;

  const fragmentShaderSource = `
    precision highp float;

    uniform vec2 u_resolution;
    uniform float u_time;
    uniform vec2 u_mouse;
    uniform vec3 u_baseColor;
    uniform float u_amplitude;

    const mat2 m = mat2(0.80, 0.60, -0.60, 0.80);

    float hash(vec2 p) {
      float h = dot(p, vec2(127.1, 311.7));
      return fract(sin(h) * 43758.5453123);
    }

    float noise(in vec2 p) {
      vec2 i = floor(p);
      vec2 f = fract(p);
      vec2 u = f * f * (3.0 - 2.0 * f);
      return mix(
        mix(hash(i + vec2(0.0, 0.0)), hash(i + vec2(1.0, 0.0)), u.x),
        mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x),
        u.y
      );
    }

    float fbm(vec2 p) {
      float f = 0.0;
      f += 0.5000 * noise(p); p = m * p * 2.02;
      f += 0.2500 * noise(p); p = m * p * 2.03;
      f += 0.1250 * noise(p); p = m * p * 2.01;
      f += 0.0625 * noise(p);
      return f / 0.9375;
    }

    void main() {
      vec2 uv = gl_FragCoord.xy / u_resolution.xy;
      vec2 p = -1.0 + 2.0 * uv;
      if (u_resolution.y > 0.0) {
        p.x *= u_resolution.x / u_resolution.y;
      }

      // Mouse interactive fluid distortion
      vec2 mouse = (u_mouse - 0.5) * 2.0;
      if (u_resolution.y > 0.0) {
        mouse.x *= u_resolution.x / u_resolution.y;
      }

      vec2 diff = p - mouse;
      float dist = length(diff);
      vec2 distortion = vec2(0.0);
      if (dist > 0.0) {
        distortion = (diff / dist) * exp(-dist * 2.5) * 0.12;
      }
      p += distortion;

      float time = u_time * 0.4;

      // Liquid Chrome Domain Warping
      vec2 q = vec2(0.0);
      q.x = fbm(p + vec2(0.0, 0.0) + time * 0.08);
      q.y = fbm(p + vec2(5.2, 1.3) + time * 0.12);

      vec2 r = vec2(0.0);
      r.x = fbm(p + 4.0 * q + vec2(1.7, 9.2) + time * 0.15);
      r.y = fbm(p + 4.0 * q + vec2(8.3, 2.8) + time * 0.18);

      float f = fbm(p + r * 3.5 * u_amplitude);

      // Mix Liquid Chrome Silver with Silk Aurora Palette (Electric Cyan & Satin Violet/Gold)
      vec3 auroraCyan = vec3(0.03, 0.42, 0.65);
      vec3 auroraGold = vec3(0.85, 0.55, 0.12);
      vec3 voidDark   = vec3(0.04, 0.06, 0.10);

      vec3 base = mix(voidDark, auroraCyan, sin(time * 0.3 + p.y * 1.5) * 0.5 + 0.5);

      float highlight = smoothstep(0.35, 0.65, f);
      float specular  = smoothstep(0.65, 0.85, f);
      float dark      = smoothstep(0.10, 0.30, f);

      vec3 col = mix(base, u_baseColor, 0.3);
      col = mix(col, voidDark, 1.0 - dark);
      col = mix(col, mix(auroraCyan, auroraGold, sin(time * 0.2 + p.x) * 0.5 + 0.5), highlight * 0.7);
      col = mix(col, vec3(0.92, 0.95, 1.0), specular * 0.85); // Pearlescent highlights

      // Subtle fine grain for Silk Aurora texture
      float grain = (hash(gl_FragCoord.xy + u_time) - 0.5) * 0.03;
      col += grain;

      // Soft Vignette
      float v = 16.0 * uv.x * uv.y * (1.0 - uv.x) * (1.0 - uv.y);
      col *= 0.35 + 0.65 * pow(max(0.0, v), 0.25);

      gl_FragColor = vec4(col, 0.85);
    }
  `;

  function initHeroCanvas() {
    const canvas = document.createElement("canvas");
    canvas.id = "heroBgCanvas";
    canvas.style.cssText = `
      position: fixed;
      top: 0;
      left: 0;
      width: 100vw;
      height: 100vh;
      z-index: -1;
      pointer-events: none;
      opacity: 0.65;
    `;
    document.body.prepend(canvas);

    const gl = canvas.getContext("webgl");
    if (!gl) return;

    function createShader(gl, type, source) {
      const shader = gl.createShader(type);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      return shader;
    }

    const vs = createShader(gl, gl.VERTEX_SHADER, vertexShaderSource);
    const fs = createShader(gl, gl.FRAGMENT_SHADER, fragmentShaderSource);

    const program = gl.createProgram();
    gl.attachShader(program, vs);
    gl.attachShader(program, fs);
    gl.linkProgram(program);
    gl.useProgram(program);

    const posBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, posBuffer);
    gl.bufferData(
      gl.ARRAY_BUFFER,
      new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]),
      gl.STATIC_DRAW
    );

    const posLoc = gl.getAttribLocation(program, "position");
    gl.enableVertexAttribArray(posLoc);
    gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);

    const resLoc = gl.getUniformLocation(program, "u_resolution");
    const timeLoc = gl.getUniformLocation(program, "u_time");
    const mouseLoc = gl.getUniformLocation(program, "u_mouse");
    const colorLoc = gl.getUniformLocation(program, "u_baseColor");
    const ampLoc = gl.getUniformLocation(program, "u_amplitude");

    let mouse = [0.5, 0.5];
    let startTime = performance.now();

    function resize() {
      const dpr = window.devicePixelRatio || 1;
      canvas.width = window.innerWidth * dpr;
      canvas.height = window.innerHeight * dpr;
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.uniform2f(resLoc, canvas.width, canvas.height);
    }

    window.addEventListener("resize", resize);
    window.addEventListener("mousemove", (e) => {
      mouse[0] = e.clientX / window.innerWidth;
      mouse[1] = 1.0 - e.clientY / window.innerHeight;
    });

    resize();

    function render(now) {
      const elapsedTime = (now - startTime) * 0.001;
      gl.uniform1f(timeLoc, elapsedTime);
      gl.uniform2f(mouseLoc, mouse[0], mouse[1]);
      gl.uniform3f(colorLoc, 0.12, 0.15, 0.22); // Silk Chrome Base Tint
      gl.uniform1f(ampLoc, 0.55);

      gl.drawArrays(gl.TRIANGLES, 0, 6);
      requestAnimationFrame(render);
    }

    requestAnimationFrame(render);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initHeroCanvas);
  } else {
    initHeroCanvas();
  }
})();
