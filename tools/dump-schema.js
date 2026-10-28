// Dump the helper IPC type map from the main bundle as JSON and list request/response types.
// Usage: node dump-schema.js path/to/.webpack/main/index.js out.json
const fs=require('fs');
const s=fs.readFileSync(process.argv[2],'utf8');
const start=s.indexOf('const Q={HelperAPI:')+'const Q='.length;
let d=0,i=start,q=null;
for(;i<s.length;i++){const c=s[i];
 if(q){if(c==='\\'){i++;continue} if(c===q)q=null;continue}
 if(c==='"'||c==="'"||c==='`'){q=c;continue}
 if(c==='{')d++; else if(c==='}'){d--; if(d===0){i++;break}}}
const src=s.slice(start,i);
function P(e){return{literal:e}}function q2(e){return{arrayItems:e}}function W(...e){return{unionMembers:e}}function F(e,t){return{props:e,additional:t}}function U(e){return{props:[],additional:e}}function j(e){return{ref:e}}
const Q=new Function('P','q','W','F','U','j','return '+src)(P,q2,W,F,U,j);
fs.writeFileSync(process.argv[3],JSON.stringify(Q,(k,v)=>v===Date?'Date':v===undefined?'undefined':v,1));
const fmt=t=>t===undefined?'undef':t===null?'null':typeof t==='string'?t:t===Date?'Date':t.ref?t.ref:t.unionMembers?t.unionMembers.map(fmt).filter(x=>x!=='undef').join('|')+(t.unionMembers.includes(undefined)?'?':''):t.arrayItems?fmt(t.arrayItems)+'[]':t.props?'{obj}':Array.isArray(t)?'enum('+t.join(',')+')':JSON.stringify(t);
for(const top of ['HelperAPIRequest','HelperAPIResponse']){
 console.log('==',top);
 for(const p of Q[top].props) console.log('  ',p.json,':',fmt(p.typ));
}
