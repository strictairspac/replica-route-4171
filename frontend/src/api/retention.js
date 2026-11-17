import {api} from './client.js';
export const retentionApi={
  listTemplates:()=>api('/retention_templates'),
  createTemplate:data=>api('/retention_templates','POST',data),
  updateTemplate:(id,data)=>api('/retention_templates/'+id,'PUT',data),
  listDrills:()=>api('/retention_drills'),
  createDrill:data=>api('/retention_drills','POST',data),
  drill:id=>api('/retention_drills/'+id),
};
