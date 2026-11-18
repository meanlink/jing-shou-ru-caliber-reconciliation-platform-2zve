import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
export const alerts={
 postBatch:data=>api('/alerts/batches',{method:'POST',body:JSON.stringify(data)}),
 ledger:channel=>api('/alerts/ledger'+(channel?'?channel='+encodeURIComponent(channel):'')),
 segments:includeClosed=>api('/alerts/segments'+(includeClosed?'?include_closed=true':'')),
 trace:key=>api('/alerts/dispatches/trace?dispatch_key='+encodeURIComponent(key)),
 inbox:()=>api('/alerts/inbox'),
 markRead:id=>api('/alerts/inbox/'+id+'/read',{method:'POST'}),
};
