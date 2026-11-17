import {retentionApi} from '../api/retention.js';
export const retentionStore={
  templates:[],drills:[],
  async refresh(){
    [this.templates,this.drills]=await Promise.all([
      retentionApi.listTemplates(),retentionApi.listDrills()]);
    return this;
  },
};
