#import <Foundation/Foundation.h>

/// Swift cannot catch an ObjC exception, and WebKit throws one - fatally -
/// when a URL-scheme task is answered a moment after WebKit stopped it.
/// This is the one place we need @try, so it lives in ObjC.
NS_ASSUME_NONNULL_BEGIN

@interface VivObjC : NSObject
+ (BOOL)catching:(void (^)(void))block;
@end

NS_ASSUME_NONNULL_END
