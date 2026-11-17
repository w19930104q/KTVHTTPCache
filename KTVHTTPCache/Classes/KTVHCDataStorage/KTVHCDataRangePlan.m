//
//  KTVHCDataRangePlan.m
//  KTVHTTPCache
//
//  Created by Single on 2017/8/11.
//  Copyright © 2017年 Single. All rights reserved.
//

#import "KTVHCDataRangePlan.h"
#import "KTVHCData+Internal.h"
#import "KTVHCDataUnit.h"
#import "KTVHCLog.h"

/**
 *  Shared range mapping. Used at plan time and again when the response
 *  range is resolved, so every resource kind shares one semantic :
 *
 *  - Open ended ranges (bytes=N-) are ensured to totalLength - 1.
 *  - Suffix ranges (bytes=-N) are normalized to the last N bytes.
 *  - Everything else is left untouched.
 */
static KTVHCRange KTVHCDataRangePlanNormalizeRange(KTVHCRange range, long long totalLength)
{
    if (totalLength > 0 && range.start == KTVHCNotFound && range.end != KTVHCNotFound) {
        long long length = MIN(range.end, totalLength);
        return KTVHCMakeRange(totalLength - length, totalLength - 1);
    }
    return KTVHCRangeWithEnsureLength(range, totalLength);
}

@implementation KTVHCDataRangePlanSegment

+ (instancetype)fileSegmentWithRange:(KTVHCRange)range readRange:(KTVHCRange)readRange path:(NSString *)path
{
    KTVHCDataRangePlanSegment *obj = [[KTVHCDataRangePlanSegment alloc] init];
    obj->_type = KTVHCDataRangePlanSegmentTypeFile;
    obj->_range = range;
    obj->_readRange = readRange;
    obj->_path = [path copy];
    return obj;
}

+ (instancetype)networkSegmentWithRange:(KTVHCRange)range
{
    KTVHCDataRangePlanSegment *obj = [[KTVHCDataRangePlanSegment alloc] init];
    obj->_type = KTVHCDataRangePlanSegmentTypeNetwork;
    obj->_range = range;
    obj->_readRange = range;
    return obj;
}

- (NSString *)description
{
    return [NSString stringWithFormat:@"<%@: %p, type : %ld, range : %@, readRange : %@>",
            self.class, self, (long)self.type,
            KTVHCStringFromRange(self.range), KTVHCStringFromRange(self.readRange)];
}

@end

@implementation KTVHCDataRangePlan

- (instancetype)initWithRequest:(KTVHCDataRequest *)request
                  originalRange:(KTVHCRange)originalRange
                    totalLength:(long long)totalLength
                       segments:(NSArray<KTVHCDataRangePlanSegment *> *)segments
{
    if (self = [super init]) {
        KTVHCLogAlloc(self);
        self->_request = request;
        self->_originalRange = originalRange;
        self->_range = request.range;
        self->_totalLength = totalLength;
        self->_segments = [segments copy];
    }
    return self;
}

- (void)dealloc
{
    KTVHCLogDealloc(self);
}

- (long long)contentLength
{
    return KTVHCRangeGetLength(self.range);
}

- (KTVHCRange)responseRangeWithTotalLength:(long long)totalLength
{
    return KTVHCDataRangePlanNormalizeRange(self.originalRange, totalLength);
}

- (NSDictionary *)responseHeadersWithBaseHeaders:(NSDictionary *)baseHeaders totalLength:(long long)totalLength
{
    KTVHCRange range = [self responseRangeWithTotalLength:totalLength];
    return KTVHCRangeFillToResponseHeaders(range, baseHeaders, totalLength);
}

- (NSDictionary *)HTTPHeadersWithBaseHeaders:(NSDictionary *)baseHeaders
{
    NSMutableDictionary *headers = [baseHeaders mutableCopy];
    [headers removeObjectForKey:@"Content-Range"];
    [headers removeObjectForKey:@"content-range"];
    [headers removeObjectForKey:@"Content-Length"];
    [headers removeObjectForKey:@"content-length"];
    return headers;
}

- (NSString *)description
{
    return [NSString stringWithFormat:@"<%@: %p, range : %@, totalLength : %lld, segments : %@>",
            self.class, self, KTVHCStringFromRange(self.range), self.totalLength, self.segments];
}

@end

@implementation KTVHCDataRangePlanner

+ (instancetype)planner
{
    static KTVHCDataRangePlanner *obj = nil;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        obj = [[self alloc] init];
    });
    return obj;
}

- (KTVHCDataRangePlan *)planWithRequest:(KTVHCDataRequest *)request unit:(KTVHCDataUnit *)unit
{
    KTVHCRange originalRange = request.range;
    long long totalLength = unit.totalLength;
    KTVHCRange range = KTVHCDataRangePlanNormalizeRange(originalRange, totalLength);
    KTVHCDataRequest *plannedRequest = [request newRequestWithRange:range];
    NSMutableArray<KTVHCDataRangePlanSegment *> *fileSegments = [NSMutableArray array];
    long long min = range.start;
    long long max = range.end;
    for (KTVHCDataUnitItem *item in unit.unitItems) {
        long long itemMin = item.offset;
        long long itemMax = item.offset + item.length - 1;
        if (itemMax < min || itemMin > max) {
            continue;
        }
        if (min > itemMin) {
            itemMin = min;
        }
        if (max < itemMax) {
            itemMax = max;
        }
        min = itemMax + 1;
        KTVHCRange itemRange = KTVHCMakeRange(item.offset, item.offset + item.length - 1);
        KTVHCRange readRange = KTVHCMakeRange(itemMin - item.offset, itemMax - item.offset);
        KTVHCDataRangePlanSegment *segment = [KTVHCDataRangePlanSegment fileSegmentWithRange:itemRange readRange:readRange path:item.absolutePath];
        [fileSegments addObject:segment];
    }
    [fileSegments sortUsingComparator:^NSComparisonResult(KTVHCDataRangePlanSegment *obj1, KTVHCDataRangePlanSegment *obj2) {
        if (obj1.range.start < obj2.range.start) {
            return NSOrderedAscending;
        }
        return NSOrderedDescending;
    }];
    NSMutableArray<KTVHCDataRangePlanSegment *> *networkSegments = [NSMutableArray array];
    long long offset = range.start;
    long long length = KTVHCRangeGetLength(range);
    for (KTVHCDataRangePlanSegment *obj in fileSegments) {
        long long delta = obj.range.start + obj.readRange.start - offset;
        if (delta > 0) {
            KTVHCRange networkRange = KTVHCMakeRange(offset, offset + delta - 1);
            [networkSegments addObject:[KTVHCDataRangePlanSegment networkSegmentWithRange:networkRange]];
            offset += delta;
            length -= delta;
        }
        offset += KTVHCRangeGetLength(obj.readRange);
        length -= KTVHCRangeGetLength(obj.readRange);
    }
    if (length > 0) {
        KTVHCRange networkRange = KTVHCMakeRange(offset, range.end);
        [networkSegments addObject:[KTVHCDataRangePlanSegment networkSegmentWithRange:networkRange]];
    }
    NSMutableArray<KTVHCDataRangePlanSegment *> *segments = [NSMutableArray array];
    [segments addObjectsFromArray:fileSegments];
    [segments addObjectsFromArray:networkSegments];
    KTVHCDataRangePlan *plan = [[KTVHCDataRangePlan alloc] initWithRequest:plannedRequest
                                                             originalRange:originalRange
                                                               totalLength:totalLength
                                                                  segments:segments];
    KTVHCLogDataReader(@"%p, Plan range\norignalRequest : %@\nplan : %@", self, request, plan);
    return plan;
}

@end
