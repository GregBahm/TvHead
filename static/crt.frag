uniform sampler2D u_image;
uniform vec2 u_resolution;
uniform float u_scanlines;
uniform float u_curvature;
uniform float u_rgbSeparation;
uniform float u_glow;
uniform float u_vignette;

vec3 sampleFrame(vec2 uv) {
  if (uv.x < 0.0 || uv.y < 0.0 || uv.x > 1.0 || uv.y > 1.0) return vec3(0.0);
  return texture2D(u_image, uv).rgb;
}

void main() {
  vec2 p = v_uv * 2.0 - 1.0;
  p *= 1.0 + 0.18 * u_curvature * p.yx * p.yx;
  vec2 uv = p * 0.5 + 0.5;
  if (uv.x < 0.0 || uv.y < 0.0 || uv.x > 1.0 || uv.y > 1.0) {
    gl_FragColor = vec4(0.0, 0.0, 0.0, 1.0);
    return;
  }
  vec2 separation = vec2(u_rgbSeparation * 6.0 / 720.0, 0.0);
  vec3 color = vec3(sampleFrame(uv + separation).r,
                    sampleFrame(uv).g, sampleFrame(uv - separation).b);
  vec2 spread = vec2(2.0 / 720.0, 2.0 / 540.0);
  vec3 glow = (sampleFrame(uv + vec2(spread.x, 0.0)) +
               sampleFrame(uv - vec2(spread.x, 0.0)) +
               sampleFrame(uv + vec2(0.0, spread.y)) +
               sampleFrame(uv - vec2(0.0, spread.y))) * 0.15;
  glow += (sampleFrame(uv + spread) + sampleFrame(uv - spread) +
           sampleFrame(uv + vec2(spread.x, -spread.y)) +
           sampleFrame(uv + vec2(-spread.x, spread.y))) * 0.1;
  color = 1.0 - (1.0 - color) * (1.0 - glow * u_glow * 0.55);
  float lines = min(240.0, u_resolution.y / 2.0);
  float scan = 0.5 + 0.5 * sin(uv.y * lines * 6.2831853);
  color *= 1.0 - u_scanlines * 0.7 * scan;
  float phosphor = mod(floor(gl_FragCoord.x), 3.0);
  vec3 mask = phosphor < 1.0 ? vec3(1.0, 0.8, 0.8)
            : phosphor < 2.0 ? vec3(0.8, 1.0, 0.8) : vec3(0.8, 0.8, 1.0);
  color *= mix(vec3(1.0), mask, u_scanlines);
  color *= 1.0 - u_vignette * 0.85 * pow(clamp(dot(p, p) * 0.5, 0.0, 1.0), 0.7);
  gl_FragColor = vec4(color, 1.0);
}
