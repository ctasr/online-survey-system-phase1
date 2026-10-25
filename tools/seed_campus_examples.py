"""Add labelled campus-service examples; never rewrite existing surveys or answers.

Run from the code directory: python -m tools.seed_campus_examples
Business operations use application APIs. Only newly generated demo documents
receive simulated timeline metadata; actual generation time remains recorded.
"""
import json
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bson import ObjectId
from app import create_app

SUITE = 'campus_examples_v1'


def seed(app, username='yyx'):
    db=app.extensions['mongo_client'][app.config['MONGO_DB_NAME']]
    owner=db.users.find_one({'username':username})
    if not owner:
        raise ValueError('Create the requested owner account before seeding examples.')
    prior=db.question_library.find_one({'demo_seed_suite':SUITE})
    now=(prior or {}).get('demo_generated_at') or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now=now.replace(tzinfo=timezone.utc)
    registered=owner['created_at'].replace(tzinfo=timezone.utc)
    start=max(now-timedelta(days=6),registered+timedelta(seconds=1))
    span=now-start
    if span.total_seconds()<20:
        raise ValueError('Wait at least 20 seconds after registration before generating the demo timeline.')
    def moment(fraction):
        return start+span*fraction
    def mark(collection,oid,key,**dates):
        collection.update_one({'_id':oid},{'$set':{
            'demo_seed_suite':SUITE,'demo_key':key,'demo_generated_at':now,
            'demo_timeline_simulated':True,**dates}})

    class Actor:
        def __init__(self,name,demo=False):
            self.client=app.test_client()
            user=db.users.find_one({'username':name})
            if user is None:
                password=secrets.token_hex(6)
                result=self.call('POST','/auth/register',{
                    'username':name,'password':password,'confirmation':password})
                self.id=ObjectId(result['id'])
                days=10 if name=='共享题目体验账号-校园服务' else 3+int(name[-2:])%7
                mark(db.users,self.id,name,created_at=now-timedelta(days=days))
            else:
                if demo and user.get('demo_seed_suite')!=SUITE:
                    raise ValueError('Unrelated account name collision: '+name)
                self.id=user['_id']
            with self.client.session_transaction() as session:
                session['user_id']=str(self.id)
        def call(self,method,path,data=None):
            token=self.client.get('/api/v1/csrf').get_json()['csrf_token']
            response=self.client.open('/api/v1'+path,method=method,json=data,
                                      headers={'X-CSRF-Token':token})
            if response.status_code>=400:
                raise RuntimeError(f'{method} {path}: {response.status_code}: {response.get_json()}')
            return response.get_json()
        def question(self,key,definition):
            old=db.question_library.find_one({'owner_id':self.id,'demo_seed_suite':SUITE,'demo_key':key})
            if old:
                return self.call('GET',f"/library/questions/{old['_id']}")['question']
            item=self.call('POST','/library/questions',definition)['question']
            fraction=.01+int(hashlib.sha256(key.encode()).hexdigest()[:6],16)/0xFFFFFF*.02
            created=now-timedelta(days=6) if key=='service_rating' else moment(fraction)
            mark(db.question_library,ObjectId(item['id']),key,created_at=created)
            return item

    yyx=Actor(username)
    author=Actor('共享题目体验账号-校园服务',True)
    common=author.question('service_rating',dict(type='single_choice',
        title='你对这项服务的整体体验满意吗？',required=False,
        options=['非常满意','比较满意','一般','不太满意','很不满意']))
    if not common['shared']:
        author.call('PATCH',f"/library/questions/{common['id']}/sharing",{'shared':True})
    library=[
        ('visits',dict(type='single_choice',title='过去一周，你去图书馆自习的频率是？',required=True,options=['没有去过','1～2天','3～4天','5天及以上'])),
        ('area',dict(type='multiple_choice',title='你通常在哪些区域自习？',required=True,options=['安静阅览区','小组讨论区','数字资源区（免预约）'],min_select=1,max_select=2)),
        ('booking',dict(type='single_choice',title='最近一次自习前，你预约座位了吗？',required=True,options=['预约了','没有预约'])),
        ('ease',dict(type='single_choice',title='这次座位预约顺利吗？',required=True,options=['一次就约到了','换了时段才约到','多次尝试才约到'])),
        ('hours',dict(type='number',title='最近一次在图书馆自习了几小时？',required=True,min_value=.5,max_value=12,number_statistics='measure')),
        ('note',dict(type='text',title='最近一次自习，有什么具体的不便？',required=False,max_length=240)),
        ('priority',dict(type='multiple_choice',title='如果先改善两项，你更希望是哪两项？',required=True,options=['增加电源插座','改善通风','增加安静座位','优化预约流程','延长开放时间'],exact_select=2)),
    ]
    canteen=[
        ('visits',dict(type='number',title='过去7天，你在校内食堂吃过几次饭？',required=True,min_value=0,max_value=30,integer_only=True,number_statistics='measure')),
        ('window',dict(type='multiple_choice',title='你最近常去哪些窗口？',required=True,options=['家常菜','面食','轻食','地方风味','早餐'],min_select=1,max_select=3)),
        ('wait',dict(type='number',title='最近一次午餐排队大约用了几分钟？',required=True,min_value=0,max_value=60,integer_only=True,number_statistics='measure')),
        ('cost',dict(type='number',title='最近一次午餐花了多少元？',required=True,min_value=1,max_value=80,number_statistics='measure')),
        ('note',dict(type='text',title='最近一顿饭，有什么想反馈的？',required=False,max_length=240)),
        ('priority',dict(type='multiple_choice',title='食堂下一步优先改善哪两方面？',required=True,options=['缩短排队时间','增加蔬菜选择','标清价格和分量','改善餐具清洁','增加晚间供应'],exact_select=2)),
        ('notice',dict(type='single_choice',title='你希望在哪里查看当日菜单？',required=False,options=['食堂入口屏幕','校园小程序','窗口旁价目牌','不需要提前查看'])),
    ]
    maker=[
        ('service',dict(type='single_choice',title='最近一个月，你主要使用过创客空间的哪项服务？',required=True,options=['设备借用','场地预约','活动培训','尚未使用'])),
        ('equipment',dict(type='multiple_choice',title='最近借用过哪些设备？',required=True,options=['相机或录音设备','电子工具套件','笔记本电脑','手工工具'],min_select=1,max_select=3)),
        ('guidance',dict(type='single_choice',title='领取设备时，使用和归还说明清楚吗？',required=True,options=['很清楚','基本清楚，仍需询问','不清楚'])),
        ('hours',dict(type='number',title='最近一次设备借用了多少小时？',required=True,min_value=.5,max_value=48,number_statistics='measure')),
        ('note',dict(type='text',title='预约、借用或参加培训时，有什么具体建议？',required=False,max_length=240)),
        ('priority',dict(type='multiple_choice',title='你最希望先增加哪两项支持？',required=True,options=['在线查看设备余量','提供简短操作指南','增加晚间开放','延长借用时限','增加入门培训'],exact_select=2)),
    ]
    cases=[('library','图书馆自习座位使用反馈（模拟）',library,.53,.55,True),
           ('canteen','校内食堂午餐体验（模拟）',canteen,.63,.65,False),
           ('maker','创客空间预约与借用反馈（模拟）',maker,.73,.75,False)]
    docs=[]
    sources=[]
    for key,title,definitions,created,published,closed in cases:
        old=db.surveys.find_one({'owner_id':yyx.id,'demo_seed_suite':SUITE,'demo_key':key})
        if old:
            doc=yyx.call('GET',f"/surveys/{old['_id']}")['survey']
        else:
            doc=yyx.call('POST','/surveys',{'title':title,
                'description':'希望了解具体使用情况，帮助改进校园服务。本问卷及已有答卷均为模拟体验数据，时间线用于演示，不代表真实调查。',
                'allow_anonymous':key!='maker','allow_multiple_submissions':key!='maker',
                'option_label_style':'numeric' if key=='canteen' else 'letters'})['survey']
            for qkey,definition in definitions:
                source=yyx.question(key+'_'+qkey,definition)
                sources.append(source)
                yyx.call('POST',f"/surveys/{doc['id']}/library-questions",{'question_id':source['id']})
            yyx.call('POST',f"/surveys/{doc['id']}/library-questions",{'question_id':common['id']})
            doc=yyx.call('GET',f"/surveys/{doc['id']}")['survey']
            ids=[q['id'] for q in doc['questions']]
            rules=({'library':[(0,{'option':'没有去过','target_id':ids[6]}),
                                (1,{'mode':'exact','match_options':['数字资源区（免预约）'],'target_id':ids[4]}),
                                (2,{'option':'没有预约','target_id':ids[4]})],
                    'canteen':[(0,{'operator':'eq','threshold':0,'target_id':ids[5]})],
                    'maker':[(0,{'option':'场地预约','target_id':ids[4]}),
                             (0,{'option':'活动培训','target_id':ids[4]}),
                             (0,{'option':'尚未使用','target_id':ids[5]})]})[key]
            for position,rule in rules:
                yyx.call('POST',f"/surveys/{doc['id']}/questions/{ids[position]}/jumps",rule)
            doc=yyx.call('POST',f"/surveys/{doc['id']}/publish",{})['survey']
            mark(db.surveys,ObjectId(doc['id']),key,created_at=moment(created),published_at=moment(published),updated_at=moment(published))
        docs.append((key,doc,definitions,published,closed))

    notes={
      'library':['窗边下午有点晒，百叶帘不太好调。','二楼靠墙一排插座松，电脑充不上电。','讨论区隔音还可以，想多几个白板。','',
        '晚饭后常找不到连着的座位。','预约页面没看清楼层，跑错了一次。','空调偏冷，坐久了需要外套。','数字资源区查资料挺方便。',
        '临时离座的保留时间有点短。','下雨天入口处地面比较滑。','', '希望闭馆前的提醒轻一点。','插座位置能否在座位图里标出来？'],
      'canteen':['12点20分面食窗口排了十分钟左右，希望取餐和点单分开。','青菜选择比上周多，份量也合适。','这次打包的汤漏了一点，袋口不太牢。','',
        '轻食窗口的价格牌离队伍有点远。','米饭有些硬，家常菜味道还不错。','下午一点半去，能选的菜不多了。','餐盘收纳区容易积水。',
        '不吃辣的菜希望有明显标记。','能显示小份菜的价格就更好了。','', '晚课结束后希望还有热饭。','二楼结账比较快，但座位不太好找。'],
      'maker':['相机领用时说明清楚，归还地点最好再醒目一点。','预约成功后没有看到房间号，找了一会儿。','工具套件少一根连接线，现场补上了。','',
        '培训时间和一节课冲突，想有周末场次。','设备余量能提前看到会方便很多。','想把充电线和设备放在同一个包里。','',
        '入门演示有帮助，结束后希望能回看讲义。','借用时间稍短，剪辑还没完成就到期了。','讨论空间挺安静，投屏连接有点慢。','', '预约取消入口不太容易找。']}
    ratings=['比较满意','一般','比较满意',None,'非常满意','不太满意','比较满意',None,'一般','比较满意','很不满意','非常满意','比较满意']
    used_paths={key:set() for key,*_ in cases}
    actors=[Actor(f'校园服务模拟账号{i:02d}',True) for i in range(1,13)]+[yyx]
    for case_index,(key,doc,definitions,published,closed) in enumerate(docs):
        for i,actor in enumerate(actors):
            response_key=f'{key}:{i}'
            if db.responses.find_one({'demo_seed_suite':SUITE,'demo_key':response_key}):
                continue
            qs=doc['questions']
            if key=='library':
                raw=[['3～4天','1～2天','5天及以上','没有去过','3～4天','1～2天','5天及以上','1～2天','3～4天','1～2天','5天及以上','没有去过','3～4天'][i],
                     [['安静阅览区'],['数字资源区（免预约）'],['安静阅览区','小组讨论区']][i%3],
                     '没有预约' if i in (1,5,7,9) else '预约了',
                     ['一次就约到了','换了时段才约到','多次尝试才约到'][i%3],
                     [2,1.5,4,.5,3,2.5,5,1,3.5,2,6,.5,2.5][i],notes[key][i],
                     [['增加电源插座','改善通风'],['增加安静座位','优化预约流程'],['延长开放时间','增加电源插座'],['改善通风','增加安静座位']][i%4]]
                rating=None if raw[0]=='没有去过' else ratings[i]
            elif key=='canteen':
                raw=[[8,5,11,0,6,9,4,7,10,3,12,0,6][i],
                     [['家常菜','早餐'],['面食'],['轻食','地方风味'],['家常菜','面食','早餐']][i%4],
                     [12,6,18,0,8,15,4,7,10,5,20,0,9][i],
                     [14,12.5,18,10,16,13,11.5,15,17,12,14.5,10,16.5][i],notes[key][i],
                     [['缩短排队时间','增加蔬菜选择'],['标清价格和分量','改善餐具清洁'],['增加晚间供应','缩短排队时间']][i%3],
                     ['校园小程序','食堂入口屏幕','窗口旁价目牌','不需要提前查看'][i%4]]
                rating=None if raw[0]==0 else ratings[(i+2)%13]
            else:
                raw=[['设备借用','场地预约','设备借用','尚未使用','活动培训','设备借用','设备借用','尚未使用','活动培训','设备借用','场地预约','设备借用','场地预约'][i],
                     [['相机或录音设备'],['电子工具套件','手工工具'],['笔记本电脑']][i%3],
                     ['很清楚','基本清楚，仍需询问','不清楚'][i%3],
                     [4,2,6,1,2,8,3,1,2,12,1.5,5,2][i],notes[key][i],
                     [['在线查看设备余量','提供简短操作指南'],['增加晚间开放','增加入门培训'],['延长借用时限','在线查看设备余量']][i%3]]
                rating=None if raw[0]=='尚未使用' else ratings[(i+1)%13]
            values={q['id']:value for q,value in zip(qs,raw) if value!=''}
            if rating:
                values[qs[-1]['id']]=rating
            assessment=actor.call('POST',f"/public/surveys/{doc['slug']}/assess",{'answers':values})
            if assessment['errors']:
                raise AssertionError(assessment['errors'])
            path=tuple(assessment['reachable'])
            used_paths[key].add(path)
            result=actor.call('POST',f"/public/surveys/{doc['slug']}/responses",{
                'revision':doc['revision'],'answers':{k:v for k,v in values.items() if k in path},
                'identity_mode':'anonymous' if key!='maker' and i in (1,3,8,10) else 'named'})
            # Different observed response times within each scenario, all before generation.
            fraction=published+(0.97-published)*([.11,.28,.07,.48,.32,.61,.19,.79,.55,.86,.43,.92,.97][i])
            mark(db.responses,ObjectId(result['response_id']),response_key,submitted_at=moment(fraction))
        if closed and doc['status']=='published':
            yyx.call('POST',f"/surveys/{doc['id']}/close",{})
            db.surveys.update_one({'_id':ObjectId(doc['id']),'demo_seed_suite':SUITE},
                                 {'$set':{'updated_at':moment(.99),'deadline':moment(.98)}})
        elif not closed:
            deadline=(now+timedelta(days=3+case_index)).replace(hour=12+case_index,minute=20+case_index*7,second=0,microsecond=0)
            if not db.surveys.find_one({'_id':ObjectId(doc['id'])}).get('deadline'):
                yyx.call('PATCH',f"/surveys/{doc['id']}",{'deadline':deadline.astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%dT%H:%M')})
                db.surveys.update_one({'_id':ObjectId(doc['id']),'demo_seed_suite':SUITE},{'$set':{'updated_at':moment(published)}})

    old_version=db.question_library.find_one({'owner_id':author.id,'demo_seed_suite':SUITE,'demo_key':'service_rating_v2'})
    if not old_version:
        version=author.call('POST',f"/library/questions/{common['id']}/versions",{
            **common['definition'],'title':'综合来看，这项服务符合你的预期吗？',
            'options':['超出预期','符合预期','基本符合','不太符合','暂时无法评价']})['question']
        mark(db.question_library,ObjectId(version['id']),'service_rating_v2',created_at=moment(.995))
        author.call('PATCH',f"/library/questions/{version['id']}/sharing",{'shared':True})
    for bank_key,name,prefix in [('daily','校园日常服务（模拟题库）','library_'),('food','食堂反馈（模拟题库）','canteen_'),('space','空间与设备（模拟题库）','maker_')]:
        bank=db.question_banks.find_one({'owner_id':yyx.id,'demo_seed_suite':SUITE,'demo_key':bank_key})
        if not bank:
            bank_id=ObjectId(yyx.call('POST','/library/banks',{'name':name})['id'])
            mark(db.question_banks,bank_id,bank_key,created_at=moment({'daily':.04,'food':.05,'space':.06}[bank_key]))
            bank=db.question_banks.find_one({'_id':bank_id})
        items=list(db.question_library.find({'owner_id':yyx.id,'demo_seed_suite':SUITE,'demo_key':{'$regex':'^'+prefix}}))+[{'_id':ObjectId(common['id'])}]
        for item in items:
            if item['_id'] not in bank['question_ids']:
                yyx.call('POST',f"/library/banks/{bank['_id']}/questions",{'question_id':str(item['_id'])})

    records=list(db.responses.find({'demo_seed_suite':SUITE}))
    assert len(records)==39
    assert len({r['submitted_at'] for r in records})==39
    for r in records:
        survey=db.surveys.find_one({'_id':r['survey_id']})
        assert registered.replace(tzinfo=None)<=survey['created_at']<=survey['published_at']<=r['submitted_at']<=now.replace(tzinfo=None)
        if survey.get('deadline'):
            assert r['submitted_at']<=survey['deadline']
    for key,doc,*_ in docs:
        assert yyx.client.get(f"/surveys/{doc['id']}/statistics").status_code==200
    report={'suite':SUITE,'simulated':True,'actual_generated_at':now.isoformat(),
        'new_surveys':3,'new_responses':39,'new_accounts':13,'new_banks':3,
        'distinct_submission_times':39,
        'surveys':[{'title':doc['title'],'manage':'/surveys/'+doc['id'],
                    'fill':'/survey/'+doc['slug'],'statistics':'/surveys/'+doc['id']+'/statistics'} for _,doc,*_ in docs],
        'account_totals':{'surveys':db.surveys.count_documents({'owner_id':yyx.id}),
                          'banks':db.question_banks.count_documents({'owner_id':yyx.id}),
                          'received_responses':db.responses.count_documents({'survey_id':{'$in':[s['_id'] for s in db.surveys.find({'owner_id':yyx.id},{'_id':1})]}})},
        'shared_rating_question':'/question-library/'+common['id']}
    return report


if __name__=='__main__':
    app=create_app()
    try:
        report=seed(app)
        output=Path(__file__).resolve().parents[1]/'output/校园服务模拟体验入口.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False,indent=2))
    finally:
        app.extensions['mongo_client'].close()
