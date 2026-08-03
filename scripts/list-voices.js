require('dotenv').config();

fetch('https://api.elevenlabs.io/v1/voices', {
  headers: { 'xi-api-key': process.env.ELEVENLABS_API_KEY },
})
  .then((r) => r.json())
  .then((d) => {
    const voices = d.voices || [];
    if (voices.length === 0) {
      console.log('No voices found. Response:', JSON.stringify(d).slice(0, 500));
      return;
    }
    for (const v of voices) {
      const lang = (v.labels && (v.labels.language || v.labels.accent)) || '';
      console.log(v.voice_id, '|', v.name, '|', lang);
    }
  })
  .catch((e) => console.log('ERR', e.message));
