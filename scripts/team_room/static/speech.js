/* Read published bot replies with the browser's available speech voices. */
const officeSpeech=(()=>{
  const synth=window.speechSynthesis,key='office-speech-settings';
  let settings={enabled:false,language:'bg-BG',voice:''};
  try{const saved=JSON.parse(localStorage.getItem(key));if(saved){settings.enabled=saved.enabled===true;if(typeof saved.language==='string')settings.language=saved.language;if(typeof saved.voice==='string')settings.voice=saved.voice;}}catch{}
  let active=null,utterance=null,queue=[],channel=null,seen=null;
  function save(){try{localStorage.setItem(key,JSON.stringify(settings));}catch{}}
  function status(text){const element=document.getElementById('audio-status');if(element)element.textContent=text;}
  function update(){
    document.querySelectorAll('.read-aloud').forEach(button=>{button.textContent=button.dataset.speechId===active?'Stop reading':'Read aloud';button.setAttribute('aria-pressed',String(button.dataset.speechId===active));});
    const toggle=document.getElementById('auto-read');if(toggle){toggle.textContent=settings.enabled?'Audio on':'Audio off';toggle.setAttribute('aria-pressed',String(settings.enabled));toggle.title=settings.enabled?'Disable automatic reading of bot replies':'Enable automatic reading of bot replies';}
  }
  function stop(){queue=[];active=null;utterance=null;if(synth)synth.cancel();update();status('Stopped.');}
  function enter(next){stop();channel=next;seen=null;}
  function speak(id,text){
    if(!synth||!window.SpeechSynthesisUtterance)return;
    if(typeof officeMedia!=='undefined')officeMedia.stop();
    const plain=text.replace(/```[^\n]*\n([\s\S]*?)```/g,'$1').replace(/\[([^\]]+)\]\([^)]*\)/g,'$1').replace(/[*_`#]/g,'');
    const speech=new SpeechSynthesisUtterance(plain);utterance=speech;active=id;speech.lang=settings.language;
    const voices=synth.getVoices();
    speech.voice=voices.find(voice=>voice.voiceURI===settings.voice)||voices.find(voice=>voice.lang.toLowerCase()===settings.language.toLowerCase())||voices.find(voice=>voice.lang.split('-')[0]===settings.language.split('-')[0])||null;
    if(speech.voice)speech.lang=speech.voice.lang;
    status(speech.voice?'Reading…':'Reading with the browser default voice.');
    speech.onend=()=>{if(utterance!==speech)return;active=null;utterance=null;update();status('Finished.');next();};
    speech.onerror=event=>{if(utterance!==speech)return;stop();status(`Read aloud unavailable: ${event.error}`);};
    update();try{synth.speak(speech);}catch(error){stop();status(error.message);}
  }
  function next(){if(!utterance&&settings.enabled&&queue.length){const reply=queue.shift();speak(reply.id,reply.text);}}
  function observe(scope,messages,ready=true){
    if(channel!==scope)enter(scope);
    if(seen===null){seen=new Set(messages.map(message=>message.id));return;}
    for(const message of messages){
      if(seen.has(message.id))continue;
      if(!ready&&settings.enabled&&message.sender!=='owner'&&message.phase!=='commentary')continue;
      seen.add(message.id);
      if(settings.enabled&&message.sender!=='owner'&&message.phase!=='commentary'&&message.text)queue.push({id:scope+':'+message.id,text:message.text});
    }
    next();
  }
  function initialize(){
    const controls=document.getElementById('audio-controls');if(!controls)return;
    const language=document.createElement('select');language.id='audio-language';language.setAttribute('aria-label','Read aloud language');
    const voices=document.createElement('select');voices.id='audio-voice';voices.setAttribute('aria-label','Read aloud voice');
    const languageLabel=document.createElement('label');languageLabel.textContent='Language';languageLabel.append(language);
    const voiceLabel=document.createElement('label');voiceLabel.textContent='Voice';voiceLabel.append(voices);
    const notice=document.createElement('span');notice.id='audio-status';notice.setAttribute('role','status');
    const cancel=document.createElement('button');cancel.type='button';cancel.className='secondary';cancel.textContent='Stop audio';cancel.onclick=stop;
    controls.replaceChildren(languageLabel,voiceLabel,cancel,notice);
    const toggle=document.getElementById('auto-read');if(toggle)toggle.onclick=()=>{settings.enabled=!settings.enabled;save();if(!settings.enabled)stop();update();};
    update();
    if(!synth||!window.SpeechSynthesisUtterance){notice.textContent='Read aloud unavailable.';cancel.disabled=true;language.disabled=true;voices.disabled=true;if(toggle)toggle.disabled=true;return;}
    function populate(){
      const available=synth.getVoices(),languages=[...new Set(['bg-BG','en-US',settings.language,...available.map(voice=>voice.lang)])].sort();
      let names;try{names=new Intl.DisplayNames(['en'],{type:'language'});}catch{}
      language.replaceChildren(...languages.map(code=>new Option(`${names?.of(code)||code} (${code})`,code)));language.value=settings.language;
      voices.replaceChildren(new Option('Automatic voice',''));
      for(const voice of available.filter(voice=>voice.lang.toLowerCase()===settings.language.toLowerCase()))voices.append(new Option(voice.name,voice.voiceURI));
      voices.value=[...voices.options].some(option=>option.value===settings.voice)?settings.voice:'';
    }
    language.onchange=()=>{stop();settings.language=language.value;settings.voice='';save();populate();};
    voices.onchange=()=>{stop();settings.voice=voices.value;save();};
    populate();synth.addEventListener?.('voiceschanged',populate);window.addEventListener('pagehide',stop);
  }
  function button(id,text){
    const button=document.createElement('button');button.type='button';button.className='read-aloud';button.dataset.speechId=id;button.textContent=id===active?'Stop reading':'Read aloud';button.setAttribute('aria-pressed',String(id===active));button.disabled=!synth||!window.SpeechSynthesisUtterance;
    button.onclick=()=>{const playing=active===id;stop();if(!playing)speak(id,text);};return button;
  }
  return {initialize,button,stop,enter,observe};
})();
