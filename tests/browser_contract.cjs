// Fast tests of the shipped browser logic; the actual download is tested separately in ego.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {webcrypto}=require('node:crypto');
const html=fs.readFileSync(require('node:path').join(__dirname,'../assets/invite_template.html'),'utf8');
const script=html.split('<script>')[1].split("$('export').addEventListener")[0];
const cfg={fields:[{id:'name',type:'text',required:true},{id:'photo',type:'image_attachment',required:true}],retention_until:'2099-01-01'};
let reads=0;
const elements={cfg:{textContent:JSON.stringify(cfg)},f_name:{value:'合成测试',focus(){}},f_photo:{files:[],focus(){}},e_name:{},e_photo:{},status:{textContent:'正在本地加密'},consent:{checked:true}};
const context=vm.createContext({document:{getElementById:id=>elements[id]},TextEncoder,crypto:webcrypto,Uint8Array,atob,btoa});
vm.runInContext(script,context);
const file=(type='image/png',size=3)=>({name:'synthetic.png',type,size,arrayBuffer:async()=>{reads++;return Uint8Array.from([1,2,3]).buffer;}});
async function rejected(files){elements.f_photo.files=files;reads=0;await assert.rejects(vm.runInContext('collectPayload()',context));assert.equal(reads,0);}
(async()=>{
 await rejected([file('image/png',6*1024*1024)]);
 await rejected([file('text/plain')]);
 await rejected([file(),file()]);
 elements.consent.checked=false;await rejected([file()]);
 elements.consent.checked=true;elements.f_name.value='';await rejected([file()]);
 elements.f_name.value='合成测试';elements.f_photo.files=[file()];reads=0;
 const payload=await vm.runInContext('collectPayload()',context);assert.equal(reads,1);assert.equal(payload.attachments.photo[0].size,3);
 const dateExpr=html.match(/if\(new Date\(\)>new Date\((.+)\)\)throw new Error\('任务已超过保存期限/)[1];
 for(const [raw,expected] of [['2099-01-01','2099-01-01T23:59:59.000Z'],['2099-01-01T23:59:59+08:00','2099-01-01T15:59:59.000Z']]){
  assert.equal(new Date(Function('cfg','return '+dateExpr)({retention_until:raw})).toISOString(),expected);
 }
 console.log('PASS: file size/type/count, consent, required fields, zero premature reads, valid payload, retention date/timezone');
})().catch(error=>{console.error(error);process.exit(1);});
