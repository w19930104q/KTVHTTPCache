//
//  KTVHCDataRangePlan.h
//  KTVHTTPCache
//
//  Created by Single on 2017/8/11.
//  Copyright © 2017年 Single. All rights reserved.
//

#import <Foundation/Foundation.h>
#import "KTVHCRange.h"

@class KTVHCDataRequest;
@class KTVHCDataUnit;

/**
 *  A segment is one continuous interval of the request range.
 *  The union of all segments covers the planned request range exactly once,
 *  in ascending order. Single-file, multi-segment and HLS manifest resources
 *  are all described by the same segment mapping.
 */
typedef NS_ENUM(NSInteger, KTVHCDataRangePlanSegmentType) {
    KTVHCDataRangePlanSegmentTypeFile,      // Read from a local cache file.
    KTVHCDataRangePlanSegmentTypeNetwork,   // Download from the remote server.
};

@interface KTVHCDataRangePlanSegment : NSObject

+ (instancetype)new NS_UNAVAILABLE;
- (instancetype)init NS_UNAVAILABLE;

+ (instancetype)fileSegmentWithRange:(KTVHCRange)range readRange:(KTVHCRange)readRange path:(NSString *)path;
+ (instancetype)networkSegmentWithRange:(KTVHCRange)range;

@property (nonatomic, readonly) KTVHCDataRangePlanSegmentType type;

/**
 *  Absolute interval in the remote content.
 */
@property (nonatomic, readonly) KTVHCRange range;

/**
 *  File segments only. Interval relative to the beginning of the cache file.
 */
@property (nonatomic, readonly) KTVHCRange readRange;

/**
 *  File segments only. Absolute path of the cache file.
 */
@property (nonatomic, copy, readonly) NSString *path;

@end

/**
 *  The result of range planning. Pure value object, safe to reuse.
 */
@interface KTVHCDataRangePlan : NSObject

+ (instancetype)new NS_UNAVAILABLE;
- (instancetype)init NS_UNAVAILABLE;

- (instancetype)initWithRequest:(KTVHCDataRequest *)request
                  originalRange:(KTVHCRange)originalRange
                    totalLength:(long long)totalLength
                       segments:(NSArray<KTVHCDataRangePlanSegment *> *)segments NS_DESIGNATED_INITIALIZER;

/**
 *  The request with the planned (ensured and normalized) range applied.
 */
@property (nonatomic, strong, readonly) KTVHCDataRequest *request;

/**
 *  The range as it was found in the original request, before any
 *  ensuring or normalization. Used to re-resolve the response range
 *  once the total length becomes known.
 */
@property (nonatomic, readonly) KTVHCRange originalRange;

/**
 *  The planned range. Equal to request.range.
 */
@property (nonatomic, readonly) KTVHCRange range;

/**
 *  The total length of the remote content at plan time. 0 means unknown.
 */
@property (nonatomic, readonly) long long totalLength;

@property (nonatomic, copy, readonly) NSArray<KTVHCDataRangePlanSegment *> *segments;

/**
 *  The number of bytes the planned range covers. KTVHCNotFound when unknown.
 */
- (long long)contentLength;

/**
 *  Resolves the response range for the given total length.
 *  Applies the same mapping as plan time : open ranges are ensured and
 *  suffix ranges (bytes=-N) are normalized to absolute offsets.
 */
- (KTVHCRange)responseRangeWithTotalLength:(long long)totalLength;

/**
 *  Response headers carrying the planned Content-Length and Content-Range.
 */
- (NSDictionary *)responseHeadersWithBaseHeaders:(NSDictionary *)baseHeaders totalLength:(long long)totalLength;

/**
 *  Headers for the HTTP connection layer. Content-Length and Content-Range
 *  are removed because the connection derives them from the planned range.
 */
- (NSDictionary *)HTTPHeadersWithBaseHeaders:(NSDictionary *)baseHeaders;

@end

/**
 *  Injectable range planning. Implement this protocol to customize how a
 *  request range is mapped to file and network segments.
 */
@protocol KTVHCDataRangePlanning <NSObject>

- (KTVHCDataRangePlan *)planWithRequest:(KTVHCDataRequest *)request unit:(KTVHCDataUnit *)unit;

@end

/**
 *  The default planner. Stateless, so the same request and cache state
 *  always produce the same plan, including after the reader and its
 *  sources have been released and requested again.
 */
@interface KTVHCDataRangePlanner : NSObject <KTVHCDataRangePlanning>

+ (instancetype)planner;

@end
