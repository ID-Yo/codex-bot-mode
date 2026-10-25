/* Read published bot replies with the browser's available speech voices. */
const officeSpeech=(()=>{
  const synth=window.speechSynthesis,key='office-speech-settings';
  let settings={enabled:false,language:'bg-BG',voice:''};
  try{const saved=JSON.parse(localStorage.getItem(key));if(saved){settings.enabled=saved.enabled===true;if(typeof saved.language==='string')settings.language=saved.language;if(typeof saved.voice==='string')settings.voice=saved.voice;}}catch{}
  let active=null,utterance=null,queue=[],channel=null,seen=null;
  function save(){try{localStorage.setItem(key,JSON.stringify(settings));}catch{}}
  // The status is kept as a dictionary key so it can be translated again when the UI language changes.
  let current=null;
  function paintStatus(){const element=document.getElementById('audio-status');if(element)element.textContent=current?(current.raw?current.key:officeI18n.t(current.key,current.values)):'';}
  function status(key,values){current={key,values};paintStatus();}
  function statusRaw(text){current={key:text,raw:true};paintStatus();}
  function update(){
    document.querySelectorAll('.read-aloud').forEach(button=>{button.textContent=button.dataset.speechId===active?officeI18n.t('Stop reading'):officeI18n.t('Read aloud');button.setAttribute('aria-pressed',String(button.dataset.speechId===active));});
    const toggle=document.getElementById('auto-read');if(toggle){toggle.textContent=settings.enabled?officeI18n.t('Audio on'):officeI18n.t('Audio off');toggle.setAttribute('aria-pressed',String(settings.enabled));toggle.title=officeI18n.t(settings.enabled?'Disable automatic reading of bot replies':'Enable automatic reading of bot replies');}
  }
  function stop(){queue=[];active=null;utterance=null;if(synth)synth.cancel();update();if(synth&&window.SpeechSynthesisUtterance)status('Stopped.');}
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
    speech.onerror=event=>{if(utterance!==speech)return;stop();status('Read aloud unavailable: {reason}',{reason:event.error||officeI18n.t('Unknown')});};
    update();try{synth.speak(speech);}catch(error){stop();statusRaw(error.message);}
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
    const language=document.createElement('select');language.id='audio-language';language.setAttribute('aria-label',officeI18n.t('Read aloud language'));
    const voices=document.createElement('select');voices.id='audio-voice';voices.setAttribute('aria-label',officeI18n.t('Read aloud voice'));
    const languageLabel=document.createElement('label');languageLabel.textContent=officeI18n.t('Language');languageLabel.append(language);
    const voiceLabel=document.createElement('label');voiceLabel.textContent=officeI18n.t('Voice');voiceLabel.append(voices);
    const notice=document.createElement('span');notice.id='audio-status';notice.setAttribute('role','status');
    const cancel=document.createElement('button');cancel.type='button';cancel.className='secondary';cancel.textContent=officeI18n.t('Stop audio');cancel.onclick=stop;
    controls.replaceChildren(languageLabel,voiceLabel,cancel,notice);
    const relabel=()=>{language.setAttribute('aria-label',officeI18n.t('Read aloud language'));voices.setAttribute('aria-label',officeI18n.t('Read aloud voice'));languageLabel.firstChild.textContent=officeI18n.t('Language');voiceLabel.firstChild.textContent=officeI18n.t('Voice');cancel.textContent=officeI18n.t('Stop audio');paintStatus();};
    const toggle=document.getElementById('auto-read');if(toggle)toggle.onclick=()=>{settings.enabled=!settings.enabled;save();if(!settings.enabled)stop();update();};
    update();
    if(!synth||!window.SpeechSynthesisUtterance){status('Read aloud unavailable.');cancel.disabled=true;language.disabled=true;voices.disabled=true;if(toggle)toggle.disabled=true;window.addEventListener('office-language-changed',()=>{relabel();update();});return;}
    function populate(){
      const available=synth.getVoices(),languages=[...new Set(['bg-BG','en-US',settings.language,...available.map(voice=>voice.lang)])].sort();
      let names;try{names=new Intl.DisplayNames([officeI18n.language],{type:'language'});}catch{}
      language.replaceChildren(...languages.map(code=>new Option(`${names?.of(code)||code} (${code})`,code)));language.value=settings.language;
      voices.replaceChildren(new Option(officeI18n.t('Automatic voice'),''));
      for(const voice of available.filter(voice=>voice.lang.toLowerCase()===settings.language.toLowerCase()))voices.append(new Option(voice.name,voice.voiceURI));
      voices.value=[...voices.options].some(option=>option.value===settings.voice)?settings.voice:'';
    }
    language.onchange=()=>{stop();settings.language=language.value;settings.voice='';save();populate();};
    voices.onchange=()=>{stop();settings.voice=voices.value;save();};
    populate();window.addEventListener('office-language-changed',()=>{relabel();update();populate();});synth.addEventListener?.('voiceschanged',populate);window.addEventListener('pagehide',stop);
  }
  function button(id,text){
    const button=document.createElement('button');button.type='button';button.className='read-aloud';button.dataset.speechId=id;button.textContent=id===active?officeI18n.t('Stop reading'):officeI18n.t('Read aloud');button.setAttribute('aria-pressed',String(id===active));button.disabled=!synth||!window.SpeechSynthesisUtterance;
    button.onclick=()=>{const playing=active===id;stop();if(!playing)speak(id,text);};return button;
  }
  return {initialize,button,stop,enter,observe};
})();
