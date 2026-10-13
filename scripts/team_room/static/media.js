/* Clipboard attachments and reviewable speech drafts for both composers. */
const officeMedia=(()=>{
  const drafts=new Map(),previews=new Map(),imageUrls=new Map();let voice=null;
  const context=input=>input.id==='topic'?(typeof roomScope==='function'?roomScope():'room'):selectedPrivateBot;
  const mediaPath=scope=>scope.startsWith('room:')?'/api/rooms/'+encodeURIComponent(scope.slice(5))+'/images/room':'/api/images/'+encodeURIComponent(scope);
  const list=scope=>{if(!drafts.has(scope))drafts.set(scope,[]);return drafts.get(scope);};
  function paint(input,container){
    const scope=context(input);container.replaceChildren();
    for(const item of list(scope)){
      const figure=document.createElement('figure'),img=document.createElement('img'),remove=document.createElement('button');
      img.src=item.preview;img.alt='Attached image';remove.type='button';remove.textContent='×';remove.setAttribute('aria-label','Remove attached image');
      remove.onclick=()=>{const items=list(scope);const index=items.indexOf(item);if(index>=0)items.splice(index,1);URL.revokeObjectURL(item.preview);paint(input,container);};
      figure.append(img,remove);container.append(figure);
    }
    input.required=list(scope).length===0;
  }
  function releaseMicrophone(current){clearInterval(current.meter);clearTimeout(current.retryTimer);clearTimeout(current.finishTimer);clearTimeout(current.sessionTimer);current.stream?.getTracks().forEach(track=>track.stop());current.audio?.close().catch(()=>{});}
  async function monitorMicrophone(current){
    if(!navigator.mediaDevices?.getUserMedia)return;
    try{
      const stream=await navigator.mediaDevices.getUserMedia({audio:current.deviceId?{deviceId:{exact:current.deviceId}}:true});
      if(voice!==current){stream.getTracks().forEach(track=>track.stop());return;}
      current.stream=stream;current.device=stream.getAudioTracks()[0]?.label||'Microphone';
      if(!window.AudioContext)return stream;
      const audio=new AudioContext(),analyser=audio.createAnalyser();current.audio=audio;analyser.fftSize=256;
      audio.createMediaStreamSource(stream).connect(analyser);await audio.resume();
      if(voice!==current){releaseMicrophone(current);return;}
      const samples=new Uint8Array(analyser.fftSize);
      current.meter=setInterval(()=>{
        if(voice!==current)return;analyser.getByteTimeDomainData(samples);
        const signal=Math.sqrt(samples.reduce((sum,value)=>sum+(value-128)**2,0)/samples.length)/128;
        current.signal=Math.max(current.signal||0,signal);current.level.value=Math.min(100,Math.round(signal*500));
        current.level.title=current.device+' · input '+current.level.value+'%';
      },150);return stream;
    }catch(error){throw error;}
  }
  function stop(){const current=voice;voice=null;if(current){releaseMicrophone(current);current.level.hidden=true;current.button.textContent='Dictate';current.button.setAttribute('aria-pressed','false');current.status.textContent='Dictation finished. Review and send.';current.recognition.abort();}}
  for(const id of ['topic','personal-topic']){
    const input=$(id),form=$(id==='topic'?'composer':'personal-composer');
    const preview=document.createElement('div');preview.className='image-previews';form.prepend(preview);previews.set(id,preview);
    const controls=document.createElement('div');controls.className='media-controls';
    const button=document.createElement('button');button.type='button';button.className='dictate secondary';button.textContent='Dictate';button.setAttribute('aria-pressed','false');
    const language=document.createElement('select');language.className='speech-language';language.setAttribute('aria-label','Dictation language');
    const languages={'bg-BG':'Bulgarian','en-US':'English (US)','en-GB':'English (UK)','de-DE':'German','fr-FR':'French','es-ES':'Spanish','it-IT':'Italian','pt-PT':'Portuguese','uk-UA':'Ukrainian','ru-RU':'Russian','tr-TR':'Turkish','el-GR':'Greek','ro-RO':'Romanian','pl-PL':'Polish','ar-SA':'Arabic','hi-IN':'Hindi','ja-JP':'Japanese','ko-KR':'Korean','zh-CN':'Chinese'};
    for(const [code,label] of Object.entries(languages))language.append(new Option(label,code));
    let savedLanguage='bg-BG';try{savedLanguage=localStorage.getItem('office-dictation-language')||savedLanguage;}catch{}
    language.value=languages[savedLanguage]?savedLanguage:'bg-BG';language.title='Dictation language';
    language.onchange=()=>{stop();try{localStorage.setItem('office-dictation-language',language.value);}catch{}document.querySelectorAll('.speech-language').forEach(select=>select.value=language.value);};
    const status=document.createElement('span');status.className='media-status';status.setAttribute('role','status');
    const level=document.createElement('meter');level.className='microphone-level';level.min=0;level.max=100;level.value=0;level.hidden=true;level.setAttribute('aria-label','Microphone input level');const microphone=document.createElement('select');microphone.className='dictation-microphone';microphone.setAttribute('aria-label','Dictation microphone');microphone.append(new Option('System default microphone',''));
    const trackInput=Number((navigator.userAgent.match(/(?:Chrome|Edg)\/(\d+)/)||[])[1])>=135&&!/Android/.test(navigator.userAgent);
    if(!trackInput){microphone.disabled=true;microphone.title='This browser uses its system default microphone.';}
    let savedMicrophone='';try{savedMicrophone=localStorage.getItem('office-dictation-microphone')||'';}catch{}
    async function populateMicrophones(){
      if(!trackInput||!navigator.mediaDevices?.enumerateDevices)return;
      try{
        const devices=(await navigator.mediaDevices.enumerateDevices()).filter(device=>device.kind==='audioinput'&&!['default','communications'].includes(device.deviceId)&&device.deviceId);
        microphone.replaceChildren(new Option('System default microphone',''),...devices.map(device=>new Option(device.label||'Microphone',device.deviceId)));
        const preferred=devices.find(device=>device.deviceId===savedMicrophone)||(!savedMicrophone&&devices.find(device=>/microphone|микрофон/i.test(device.label)));
        microphone.value=preferred?.deviceId||'';
      }catch{}
    }
    microphone.onchange=()=>{stop();savedMicrophone=microphone.value;try{localStorage.setItem('office-dictation-microphone',savedMicrophone);}catch{}document.querySelectorAll('.dictation-microphone').forEach(select=>select.value=savedMicrophone);};
    populateMicrophones();navigator.mediaDevices?.addEventListener?.('devicechange',populateMicrophones);
    controls.append(button,language,microphone,level);
    const toolbar=form.querySelector('.composer-toolbar');
    (toolbar||form).prepend(controls);
    form.append(status);
    const help=form.closest('.composer-panel')?.querySelector('.room-help');
    if(toolbar&&help)toolbar.append(help);
    const Recognition=window.SpeechRecognition||window.webkitSpeechRecognition;
    if(!Recognition){button.disabled=true;status.textContent='Dictation unavailable in this browser.';}
    else button.title='Browser speech recognition may process audio online. Text stays in the draft.';
    button.onclick=()=>{
      if(voice?.button===button){if(voice.retryTimer||voice.starting){stop();return;}const current=voice;current.retry=false;current.started=0;current.recognition.stop();current.finishTimer=setTimeout(()=>{if(voice===current){stop();status.textContent=current.heard?'Dictation finished. Review and send.':'The speech service returned no text. Check the language and try again.';}},2000);return;}
      stop();if(typeof officeSpeech!=='undefined')officeSpeech.stop();const recognition=new Recognition(),current={recognition,button,status,scope:context(input),level,deviceId:microphone.value,starting:true,started:Date.now(),segments:new Map(),start:null,rendered:'',heard:false};voice=current;current.sessionTimer=setTimeout(()=>{if(voice===current){stop();status.textContent=current.heard?'Dictation finished. Review and send.':'No recognized text after one minute. Check the microphone and language.';}},60000);level.hidden=false;level.value=0;status.textContent='Starting microphone…';button.textContent='Stop dictation';button.setAttribute('aria-pressed','true');
      recognition.lang=language.value.trim()||navigator.language;recognition.continuous=true;recognition.interimResults=true;
      recognition.onstart=()=>{if(voice!==current)return;button.textContent='Stop dictation';button.setAttribute('aria-pressed','true');status.textContent='Listening: '+(current.device||'microphone')+'. Speak now…';};
      recognition.onaudiostart=()=>{if(voice===current)status.textContent='Microphone active. Speak now…';};
      recognition.onspeechstart=()=>{if(voice===current)status.textContent='Recognizing speech…';};
      recognition.onresult=event=>{
        if(voice!==current||context(input)!==current.scope)return;
        for(let i=event.resultIndex;i<event.results.length;i++)current.segments.set(i,event.results[i][0].transcript.trim());
        const transcript=[...current.segments.values()].filter(Boolean).join(' ');if(!transcript)return;
        current.heard=true;
        if(current.start===null){current.start=input.selectionStart;current.end=input.selectionEnd;current.space=current.start&&!/\s$/.test(input.value.slice(0,current.start))?' ':'';}
        if(current.rendered&&input.value.slice(current.start,current.start+current.rendered.length)!==current.rendered){stop();status.textContent='Draft edited. Start dictation again to continue.';return;}
        const replacement=current.space+transcript;
        input.setRangeText(replacement,current.start,current.rendered?current.start+current.rendered.length:current.end,'end');current.rendered=replacement;
        input.dispatchEvent(new Event('input',{bubbles:true}));status.textContent='Listening… Text is in your draft.';
      };
      recognition.onerror=event=>{if(voice!==current)return;current.failed=true;current.retry=event.error==='no-speech';status.textContent=({'not-allowed':'Allow microphone access for localhost in Chrome site settings, then try again.','audio-capture':'No microphone is available. Check the default input device in Windows and Chrome.','no-speech':'No speech detected. Check your microphone input and speak after the listening indicator.','network':'The browser speech service could not connect. Check your connection and try again.','language-not-supported':'This speech service does not support the selected language.'})[event.error]||`Dictation stopped: ${event.error}`;};
      recognition.onend=()=>{
        if(voice!==current)return;
        if(current.retry&&Date.now()-current.started<60000){
          status.textContent=current.signal>.015?'Sound detected, but no speech recognized. Still listening…':'Waiting for speech. Check the input meter while you speak.';
          current.retry=false;current.failed=false;current.segments.clear();current.start=null;current.rendered='';
          current.retryTimer=setTimeout(()=>{current.retryTimer=null;if(voice===current)try{recognition.start(trackInput?current.stream?.getAudioTracks()[0]:undefined);}catch(error){stop();status.textContent=error.message;}},250);return;
        }
        voice=null;releaseMicrophone(current);level.hidden=true;button.textContent='Dictate';button.setAttribute('aria-pressed','false');
        if(current.failed&&current.retry)status.textContent=current.signal>.015?'Microphone received sound, but the browser did not recognize speech. Try another language.':'No microphone input detected. Check the selected input device and mute controls.';
        else if(!current.failed)status.textContent=current.heard?'Dictation finished. Review and send.':'No speech received. Check the microphone input and try again.';
      };
      monitorMicrophone(current).then(()=>{
        if(voice!==current)return;current.starting=false;populateMicrophones();
        try{recognition.start(trackInput?current.stream?.getAudioTracks()[0]:undefined);}catch(error){stop();status.textContent=error.message;}
      }).catch(error=>{if(voice!==current)return;stop();status.textContent='Cannot open microphone: '+error.message;});
    };
    input.addEventListener('paste',async event=>{
      const files=[...(event.clipboardData?.items||[])].filter(item=>item.kind==='file'&&item.type.startsWith('image/')).map(item=>item.getAsFile()).filter(Boolean);
      if(!files.length)return;event.preventDefault();const scope=context(input);
      const pastedText=event.clipboardData.getData('text/plain');if(pastedText){input.setRangeText(pastedText,input.selectionStart,input.selectionEnd,'end');input.dispatchEvent(new Event('input',{bubbles:true}));}
      for(const file of files){
        if(list(scope).length>=4||file.size>8*1024*1024){status.textContent='Attach at most 4 images, each up to 8 MB.';break;}
        const item={id:null,preview:URL.createObjectURL(file)};list(scope).push(item);paint(input,preview);status.textContent='Uploading image…';
        try{
          const response=await fetch(mediaPath(scope),{method:'POST',headers:{'Content-Type':file.type,'X-Room-Token':token},body:file});
          const data=await response.json();if(!response.ok)throw Error(data.error);item.id=data.id;status.textContent='Image ready. Review and send.';
        }catch(error){const index=list(scope).indexOf(item);if(index>=0)list(scope).splice(index,1);URL.revokeObjectURL(item.preview);status.textContent=error.message;}
        if(context(input)===scope)paint(input,preview);
      }
    });
  }
  window.addEventListener('pagehide',()=>{stop();for(const items of drafts.values())for(const item of items)URL.revokeObjectURL(item.preview);for(const url of imageUrls.values())url.then(URL.revokeObjectURL).catch(()=>{});});
  return {
    stop,
    refresh(){for(const id of ['topic','personal-topic'])paint($(id),previews.get(id));},
    snapshot(scope){const items=list(scope);if(items.some(item=>!item.id))throw Error('Wait for the image upload to finish.');return items.slice();},
    clear(scope,sent){for(const item of sent){const index=list(scope).indexOf(item);if(index>=0){list(scope).splice(index,1);URL.revokeObjectURL(item.preview);}}this.refresh();},
    message(message){
      const body=officeMarkdown(message.text||'');
      for(const item of message.images||[]){
        if(!/^[a-f0-9]{32}$/.test(item.id)||!/^[a-z][a-z0-9-]{0,47}$/.test(item.scope))continue;
        const roomId=item.room_id||'development';if(!/^(development|[a-f0-9]{32})$/.test(roomId))continue;
        const key=roomId+'/'+item.scope+'/'+item.id,img=document.createElement('img');img.className='message-image';img.alt='Attached image';body.append(img);
        const path=item.scope==='room'&&roomId!=='development'?'/api/rooms/'+roomId+'/images/room/'+item.id:'/api/images/'+item.scope+'/'+item.id;
        if(!imageUrls.has(key))imageUrls.set(key,fetch(path,{headers:{'X-Room-Token':token}}).then(async response=>{if(!response.ok)throw Error('Image unavailable');return URL.createObjectURL(await response.blob());}).catch(error=>{imageUrls.delete(key);throw error;}));
        imageUrls.get(key).then(url=>{img.src=url;}).catch(()=>{img.alt='Attached image unavailable';});
      }
      return body;
    }
  };
})();
