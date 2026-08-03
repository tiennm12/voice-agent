// src/server.js
require('dotenv').config();

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { WebSocketServer } = require('ws');
const OpenAI = require('openai');

const { loadConfig } = require('./config');
const { createRealtimeSttClient } = require('./realtimeSttClient');
const { createLlmClient } = require('./llmClient');
const { createTtsClient } = require('./ttsClient');
const { createSessionOrchestrator } = require('./sessionOrchestrator');

const CONTENT_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
};

function serveStatic(req, res) {
  const publicDir = path.join(__dirname, '..', 'public');
  const requestedPath = req.url === '/' ? '/index.html' : req.url;
  const filePath = path.join(publicDir, requestedPath);

  if (!filePath.startsWith(publicDir)) {
    res.writeHead(403);
    res.end('Forbidden');
    return;
  }

  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404);
      res.end('Not found');
      return;
    }
    const ext = path.extname(filePath);
    res.writeHead(200, { 'Content-Type': CONTENT_TYPES[ext] || 'application/octet-stream' });
    res.end(data);
  });
}

function main() {
  const config = loadConfig();
  const openaiClient = new OpenAI({ apiKey: config.openaiApiKey });

  const server = http.createServer(serveStatic);
  const wss = new WebSocketServer({ server, path: '/ws' });

  wss.on('connection', (browserWs) => {
    // Short id to correlate one call's log lines in a multi-call server log.
    const sessionId = crypto.randomUUID().slice(0, 8);

    const sttClient = createRealtimeSttClient({
      apiKey: config.openaiApiKey,
      model: config.openaiRealtimeModel,
      transcribeModel: config.openaiTranscribeModel,
    });
    const llmClient = createLlmClient({ openaiClient });
    const ttsClient = createTtsClient({
      apiKey: config.elevenLabsApiKey,
      voiceId: config.elevenLabsVoiceId,
      modelId: config.elevenLabsModelId,
    });

    const orchestrator = createSessionOrchestrator({
      systemPrompt: config.systemPrompt,
      sttClient,
      llmClient,
      ttsClient,
      sessionId,
      sendControl: (msg) => {
        if (browserWs.readyState === browserWs.OPEN) browserWs.send(JSON.stringify(msg));
      },
      sendAudio: (buffer) => {
        if (browserWs.readyState === browserWs.OPEN) browserWs.send(buffer, { binary: true });
      },
    });

    console.log(`[${sessionId}] [server] new call connected`);

    orchestrator.start().catch((err) => {
      console.error(`[${sessionId}] [server] failed to start session`, err);
      browserWs.send(JSON.stringify({ type: 'error', message: 'Không thể kết nối tới dịch vụ giọng nói.' }));
      browserWs.close();
    });

    browserWs.on('message', (data, isBinary) => {
      if (isBinary) {
        orchestrator.handleAudioChunk(data);
      }
    });

    browserWs.on('close', () => {
      console.log(`[${sessionId}] [server] call ended`);
      orchestrator.stop();
    });

    browserWs.on('error', (err) => {
      console.error(`[${sessionId}] [server] browser websocket error`, err);
    });
  });

  server.listen(config.port, () => {
    console.log(`[server] listening on http://localhost:${config.port}`);
  });
}

main();

module.exports = { serveStatic };
