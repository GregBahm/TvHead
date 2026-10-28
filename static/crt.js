"use strict";

class CrtRenderer {
  constructor(canvas, fragmentSource) {
    const gl = canvas.getContext("webgl", { alpha: false, antialias: false, depth: false, stencil: false });
    if (!gl) throw new Error("WebGL is unavailable. Enable graphics acceleration in your browser.");
    this.gl = gl;
    const compile = (type, source) => {
      const shader = gl.createShader(type);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        const detail = gl.getShaderInfoLog(shader);
        gl.deleteShader(shader);
        throw new Error(`CRT shader compilation failed: ${detail}`);
      }
      return shader;
    };
    const precision = gl.getShaderPrecisionFormat(gl.FRAGMENT_SHADER, gl.HIGH_FLOAT).precision ? "highp" : "mediump";
    const vertex = compile(gl.VERTEX_SHADER, `
      attribute vec2 a_position;
      varying ${precision} vec2 v_uv;
      void main() {
        v_uv = a_position * 0.5 + 0.5;
        gl_Position = vec4(a_position, 0.0, 1.0);
      }
    `);
    let fragment;
    const program = gl.createProgram();
    try {
      fragment = compile(gl.FRAGMENT_SHADER, `
        precision ${precision} float;
        varying ${precision} vec2 v_uv;
        ${fragmentSource}
      `);
      gl.attachShader(program, vertex);
      gl.attachShader(program, fragment);
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
        throw new Error(`CRT shader linking failed: ${gl.getProgramInfoLog(program)}`);
      }
    } catch (error) {
      gl.deleteProgram(program);
      throw error;
    } finally {
      gl.deleteShader(vertex);
      if (fragment) gl.deleteShader(fragment);
    }
    gl.useProgram(program);
    this.uniforms = Object.fromEntries(
      ["resolution", "scanlines", "curvature", "rgbSeparation", "glow", "vignette"]
        .map((key) => [key, gl.getUniformLocation(program, `u_${key}`)]));
    const vertices = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, vertices);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const position = gl.getAttribLocation(program, "a_position");
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    this.texture = gl.createTexture();
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.texture);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    gl.uniform1i(gl.getUniformLocation(program, "u_image"), 0);
  }

  draw(frame, settings) {
    const gl = this.gl;
    if (gl.isContextLost()) throw new Error("The browser lost the CRT graphics context.");
    if (gl.canvas.width !== frame.width || gl.canvas.height !== frame.height) {
      gl.canvas.width = frame.width;
      gl.canvas.height = frame.height;
    }
    gl.viewport(0, 0, frame.width, frame.height);
    gl.uniform2f(this.uniforms.resolution, frame.width, frame.height);
    for (const key of ["scanlines", "curvature", "rgbSeparation", "glow", "vignette"]) {
      gl.uniform1f(this.uniforms[key], settings[key]);
    }
    gl.bindTexture(gl.TEXTURE_2D, this.texture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, frame);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
}
